"""Sherpa-onnx speaker-embedding speaker ID for Marvi's duplex voice loop.

Standalone from the wake-word code path: this module owns its own model
download/cache location, its own guarded ``sherpa_onnx`` import, and never
touches ``tools/streaming_stt.py`` or any wake-word config. It uses the
``sherpa_onnx`` pip package purely for speaker-embedding extraction -- a
different sherpa-onnx model class (``SpeakerEmbeddingExtractor``) from the
keyword-spotting model the (now-retired, LiveKit-replaced) wake word used.

Split into two layers so tests never need sherpa-onnx or network access:

- **Transport** (thin): :func:`compute_embedding` -- downloads/loads the
  ONNX model and runs inference. Never raises; returns ``None`` on any
  failure so callers degrade to "can't identify" rather than crashing.
- **Pure logic**: the JSON store (CRUD + atomic writes), cosine similarity,
  and threshold matching all work on plain embeddings (``list[float]``) and
  take no sherpa-onnx dependency at all -- tests drive them with canned
  vectors.

Store: ``~/.hermes/voice/speakers.json`` (atomic write, 0600). Multiple
embeddings may be enrolled per name; they're averaged at verify time. The
first name ever enrolled becomes the "owner" unless a name literally equals
"owner" (case-insensitive), which always claims the owner slot.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_dir, get_hermes_home

logger = logging.getLogger(__name__)

OWNER_LABEL = "owner"
GUEST_LABEL = "guest"
UNKNOWN_LABEL = "unknown"

DEFAULT_SPEAKER_MODEL_ID = "wespeaker-en-voxceleb-cam++"
DEFAULT_THRESHOLD = 0.60

# A small (~7 MB), English speaker-embedding model from sherpa-onnx's own
# speaker-recognition-models release -- same CPU ONNX runtime as the rest of
# the voice stack, unrelated to the (retired) sherpa KWS wake-word release.
_SHERPA_SPEAKER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx"
)


class SpeakerIdUnavailable(RuntimeError):
    """Raised internally when embedding computation cannot proceed.

    Never escapes the public :func:`identify`/:func:`enroll` boundary except
    from :func:`enroll` itself, where the caller (CLI) needs to know
    enrollment failed outright.
    """


# ---------------------------------------------------------------------------
# Model download/cache + guarded import (transport)
# ---------------------------------------------------------------------------


def _import_sherpa_onnx():
    try:
        import sherpa_onnx  # type: ignore
    except ImportError as exc:
        try:
            from tools.lazy_deps import ensure

            ensure("voice.speaker_id")
            import sherpa_onnx  # type: ignore
        except Exception as install_exc:
            raise SpeakerIdUnavailable(
                "Speaker ID dependency is unavailable. Run "
                "`hermes tools post-setup speaker_id`."
            ) from install_exc
    return sherpa_onnx


def _speaker_model_cache_dir() -> Path:
    return Path(get_hermes_dir(f"cache/speaker-id/{DEFAULT_SPEAKER_MODEL_ID}", "speaker_id_cache"))


def _download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out)
        tmp.replace(target)
    except (OSError, urllib.error.URLError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SpeakerIdUnavailable(f"Could not download speaker-ID model: {exc}") from exc


def resolve_speaker_model_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the ONNX speaker-embedding model path, downloading + caching
    the default model on first use.

    ``voice.speaker_id.model`` may point at a local file to use instead.
    """
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    model_value = str(cfg_get(cfg, "voice", "speaker_id", "model", default="") or "").strip()

    if model_value:
        model_path = Path(model_value).expanduser()
        if model_path.exists():
            return str(model_path)
        raise SpeakerIdUnavailable(f"voice.speaker_id.model does not exist: {model_value}")

    target = _speaker_model_cache_dir() / "model.onnx"
    if not target.exists() or target.stat().st_size == 0:
        logger.info("[SpeakerID] Downloading sherpa-onnx speaker-embedding model")
        _download_file(_SHERPA_SPEAKER_MODEL_URL, target)
    return str(target)


_extractor_cache: Dict[str, Any] = {}
_extractor_lock = threading.Lock()


def _get_extractor(model_path: str):
    sherpa_onnx = _import_sherpa_onnx()
    with _extractor_lock:
        extractor = _extractor_cache.get(model_path)
        if extractor is None:
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=model_path, num_threads=1, debug=False, provider="cpu",
            )
            extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            _extractor_cache[model_path] = extractor
        return extractor


def warm_speaker_id(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Load Sherpa's runtime first and an enrolled speaker model when present."""
    _import_sherpa_onnx()
    if not default_store_path().exists():
        return False
    _get_extractor(resolve_speaker_model_path(cfg))
    return True


def compute_embedding(
    pcm16_bytes_16k: bytes, *, cfg: Optional[Dict[str, Any]] = None,
) -> Optional[List[float]]:
    """Compute a speaker embedding from raw 16 kHz mono PCM16 audio.

    Never raises -- returns ``None`` when sherpa-onnx/the model is
    unavailable, the audio is too short, or inference fails for any reason.
    Callers treat ``None`` as "can't identify right now", not a hard error.
    """
    if not pcm16_bytes_16k:
        return None
    try:
        model_path = resolve_speaker_model_path(cfg)
        extractor = _get_extractor(model_path)
    except Exception as exc:
        logger.debug("Speaker embedding unavailable: %s", exc)
        return None

    try:
        import numpy as np

        samples = (
            np.frombuffer(pcm16_bytes_16k, dtype="<i2").astype(np.float32) / 32768.0
        )
        stream = extractor.create_stream()
        stream.accept_waveform(16000, samples.tolist())
        stream.input_finished()
        if not extractor.is_ready(stream):
            return None
        embedding = extractor.compute(stream)
        return [float(x) for x in embedding]
    except Exception:
        logger.exception("Speaker embedding computation failed")
        return None


# ---------------------------------------------------------------------------
# Store (pure -- CRUD + atomic write, no sherpa-onnx dependency)
# ---------------------------------------------------------------------------


def default_store_path() -> Path:
    return get_hermes_home() / "voice" / "speakers.json"


def _empty_store() -> Dict[str, Any]:
    return {"owner": None, "speakers": {}}


def load_store(path: Optional[Path] = None) -> Dict[str, Any]:
    path = path or default_store_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _empty_store()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    data.setdefault("owner", None)
    if not isinstance(data.get("speakers"), dict):
        data["speakers"] = {}
    return data


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".speakers-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        try:
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def enroll_embedding(
    name: str, embedding: List[float], *, path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append ``embedding`` for ``name`` to the store; return the new store.

    The first name ever enrolled becomes "owner". A name that literally
    equals "owner" (case-insensitive) always claims the owner slot, even on
    a later enrollment -- an explicit way to (re)designate ownership.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Speaker name is required")
    if not embedding:
        raise ValueError("Embedding is required")

    path = path or default_store_path()
    store = load_store(path)
    speakers: Dict[str, Any] = store["speakers"]
    key = name.lower()

    entry = speakers.setdefault(key, {"display_name": name, "embeddings": []})
    entry["display_name"] = name
    entry["embeddings"].append([float(x) for x in embedding])

    if not store.get("owner"):
        store["owner"] = key
    if key == OWNER_LABEL:
        store["owner"] = key

    _atomic_write_json(path, store)
    return store


def enroll(
    name: str,
    pcm16_bytes_16k: bytes,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compute an embedding from audio and enroll it for ``name``.

    Raises :class:`SpeakerIdUnavailable` when an embedding can't be computed
    (sherpa-onnx unavailable, model download failed, audio unusable) -- unlike
    :func:`identify`, enrollment is a deliberate user action, so failure
    should be visible rather than silently degraded.
    """
    embedding = compute_embedding(pcm16_bytes_16k, cfg=cfg)
    if embedding is None:
        raise SpeakerIdUnavailable(
            "Could not compute a speaker embedding -- sherpa-onnx may be "
            "unavailable, the model failed to download, or the audio was "
            "too short/silent."
        )
    return enroll_embedding(name, embedding, path=path)


def list_speakers(*, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    out = []
    for key, entry in sorted((store.get("speakers") or {}).items()):
        out.append(
            {
                "name": entry.get("display_name", key),
                "key": key,
                "is_owner": key == owner_key,
                "embeddings": len(entry.get("embeddings") or []),
            }
        )
    return out


def remove_speaker(name: str, *, path: Optional[Path] = None) -> bool:
    path = path or default_store_path()
    store = load_store(path)
    key = (name or "").strip().lower()
    speakers = store.get("speakers") or {}
    if key not in speakers:
        return False
    del speakers[key]
    if store.get("owner") == key:
        store["owner"] = next(iter(speakers), None)
    _atomic_write_json(path, store)
    return True


# ---------------------------------------------------------------------------
# Matching (pure -- cosine similarity + threshold)
# ---------------------------------------------------------------------------


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _average_embedding(embeddings: List[List[float]]) -> Optional[List[float]]:
    vectors = [e for e in embeddings if e]
    if not vectors:
        return None
    dim = len(vectors[0])
    sums = [0.0] * dim
    n = 0
    for vec in vectors:
        if len(vec) != dim:
            continue
        for i, v in enumerate(vec):
            sums[i] += v
        n += 1
    if n == 0:
        return None
    return [s / n for s in sums]


def identify_embedding(
    embedding: List[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    path: Optional[Path] = None,
) -> Tuple[str, float]:
    """Match ``embedding`` against enrolled speakers. Never raises.

    Returns ``("unknown", score)`` when nothing clears ``threshold`` (or the
    store is empty), ``("owner", score)`` for the enrolled owner, and
    ``("guest", score)`` for any other enrolled speaker.
    """
    path = path or default_store_path()
    store = load_store(path)
    speakers = store.get("speakers") or {}
    owner_key = store.get("owner")

    best_key: Optional[str] = None
    best_score = -1.0
    for key, entry in speakers.items():
        avg = _average_embedding(entry.get("embeddings") or [])
        if avg is None:
            continue
        score = cosine_similarity(embedding, avg)
        if score > best_score:
            best_key, best_score = key, score

    if best_key is None or best_score < threshold:
        return UNKNOWN_LABEL, max(best_score, 0.0)
    return (OWNER_LABEL if best_key == owner_key else GUEST_LABEL), best_score


def identify(
    pcm16_bytes_16k: bytes,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Tuple[str, float]:
    """Compute an embedding for ``pcm16_bytes_16k`` and identify the speaker.

    Never raises and never blocks the caller on a hard failure: any problem
    (no store, no model, bad audio) degrades to ``("unknown", 0.0)``.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg_dict = cfg if cfg is not None else load_config()
        threshold = float(
            cfg_get(cfg_dict, "voice", "speaker_id", "threshold", default=DEFAULT_THRESHOLD)
        )
    except Exception:
        cfg_dict = cfg
        threshold = DEFAULT_THRESHOLD

    try:
        store_path = path or default_store_path()
        if not store_path.exists():
            return UNKNOWN_LABEL, 0.0
        embedding = compute_embedding(pcm16_bytes_16k, cfg=cfg_dict)
        if embedding is None:
            return UNKNOWN_LABEL, 0.0
        return identify_embedding(embedding, threshold=threshold, path=store_path)
    except Exception:
        logger.exception("Speaker identify failed; returning unknown")
        return UNKNOWN_LABEL, 0.0


def require_owner_for_escalation(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """``voice.speaker_id.require_owner_for_escalation`` -- default True."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    return bool(
        cfg_get(cfg, "voice", "speaker_id", "require_owner_for_escalation", default=True)
    )
