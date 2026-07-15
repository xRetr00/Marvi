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
MIN_ENROLLMENT_SAMPLES = 3
MIN_ENROLLMENT_CONSISTENCY = 0.60

DEFAULT_SPEAKER_MODEL_ID = "wespeaker-en-voxceleb-cam++"
# 2026-07-15 fail-open round: aligned to the spec's long-standing documented
# default (docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md
# §4) -- the config default had drifted to 0.60, which is stricter than the
# spec ever called for and part of why real owner utterances (0.51-0.61
# accepted, but also 0.22-0.33 dropped) were landing below threshold so
# often. See DEFAULT_REJECT_THRESHOLD below for the new companion bound.
DEFAULT_THRESHOLD = 0.45

# Reserved store key for Marvi's own TTS-voice self-enrollment profile (spec
# Part 1.3, barge-in confirm-negative). Never claims the owner slot, never
# returned by list_speakers -- it isn't a person, it's an echo reference.
RESERVED_TTS_PROFILE_KEY = "__marvi_tts__"

# ---------------------------------------------------------------------------
# voice.speaker_id.model registry (Part 3): embeddings are model-specific, so
# every entry here is a distinct vector space -- switching ids requires
# re-enrollment (see model_mismatch()). All three are sherpa-onnx's own
# speaker-recognition-models release assets (same CPU ONNX runtime as the
# rest of the voice stack); every one is trained on VoxCeleb (English), which
# keeps them reasonably language-robust even though the label is "en".
# ---------------------------------------------------------------------------
SPEAKER_MODEL_REGISTRY: Dict[str, Dict[str, str]] = {
    # Default -- kept for compatibility with every store enrolled before this
    # registry existed (their model_id, once stamped, will read back as this
    # key -- see model_mismatch()'s "no model_id recorded yet" fallback).
    "wespeaker-en-voxceleb-cam++": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx"
        ),
        "label": "WeSpeaker CAM++ (English, VoxCeleb) -- default, ~7MB, fastest CPU inference",
    },
    # Stronger option: same WeSpeaker family, larger ResNet293 backbone with
    # a large-margin fine-tune (the "_LM" release variant) -- lower EER than
    # CAM++ on VoxCeleb1-O at the cost of a bigger model / slower CPU
    # inference. Good pick when far-field/noisy audio (the real owner
    # utterances that scored 0.22-0.33 close-mic-vs-live-speech gap this
    # round is fixing) needs more discriminative power than CAM++ gives.
    "wespeaker-en-voxceleb-resnet293-lm": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/wespeaker_en_voxceleb_resnet293_LM.onnx"
        ),
        "label": (
            "WeSpeaker ResNet293-LM (English, VoxCeleb) -- larger backbone, "
            "lower EER than CAM++, slower CPU inference"
        ),
    },
    # Alternate architecture (3D-Speaker's ERes2Net rather than WeSpeaker's
    # CAM++/ResNet family) -- also VoxCeleb-trained. Offered as a second
    # option per the design note that a single architecture family can share
    # blind spots; this is a genuinely different model, not just a bigger one.
    "3dspeaker-eres2net-en-voxceleb": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
        ),
        "label": "3D-Speaker ERes2Net (English, VoxCeleb) -- alternate architecture, also language-robust",
    },
}


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


def _speaker_model_cache_dir(model_id: str) -> Path:
    return Path(get_hermes_dir(f"cache/speaker-id/{model_id}", "speaker_id_cache"))


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


def resolve_speaker_model_id(cfg: Optional[Dict[str, Any]] = None) -> str:
    """The effective ``voice.speaker_id.model`` registry id for ``cfg``.

    Used purely for model-mismatch bookkeeping (see :func:`model_mismatch`):
    a literal local-file override (not a registry id) gets a synthetic
    ``"custom:<path>"`` id so switching between two different local files is
    still detected as a mismatch, same as switching registry ids.
    """
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    value = str(cfg_get(cfg, "voice", "speaker_id", "model", default="") or "").strip()
    if not value:
        return DEFAULT_SPEAKER_MODEL_ID
    if value in SPEAKER_MODEL_REGISTRY:
        return value
    return f"custom:{value}"


def resolve_speaker_model_path(cfg: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the ONNX speaker-embedding model path, downloading + caching
    the selected model on first use.

    ``voice.speaker_id.model`` selects a :data:`SPEAKER_MODEL_REGISTRY` id
    (default :data:`DEFAULT_SPEAKER_MODEL_ID` when unset), OR may point at an
    existing local file to use instead (checked first, so a local override
    always wins over registry lookup).
    """
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    model_value = str(cfg_get(cfg, "voice", "speaker_id", "model", default="") or "").strip()

    if model_value:
        model_path = Path(model_value).expanduser()
        if model_path.exists():
            return str(model_path)
        if model_value not in SPEAKER_MODEL_REGISTRY:
            raise SpeakerIdUnavailable(
                f"voice.speaker_id.model {model_value!r} is neither an existing local "
                "file nor a known model id ("
                + ", ".join(sorted(SPEAKER_MODEL_REGISTRY))
                + ")."
            )
        model_id = model_value
    else:
        model_id = DEFAULT_SPEAKER_MODEL_ID

    target = _speaker_model_cache_dir(model_id) / "model.onnx"
    if not target.exists() or target.stat().st_size == 0:
        logger.info("[SpeakerID] Downloading sherpa-onnx speaker-embedding model id=%s", model_id)
        _download_file(SPEAKER_MODEL_REGISTRY[model_id]["url"], target)
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
    name: str,
    embedding: List[float],
    *,
    path: Optional[Path] = None,
    model_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append ``embedding`` for ``name`` to the store; return the new store.

    The first name ever enrolled becomes "owner". A name that literally
    equals "owner" (case-insensitive) always claims the owner slot, even on
    a later enrollment -- an explicit way to (re)designate ownership.

    ``model_id`` (Part 3, :data:`SPEAKER_MODEL_REGISTRY`) stamps the store
    with which embedding model produced this sample, so a later config
    change to a different model is detected as a mismatch (see
    :func:`model_mismatch`) instead of silently comparing embeddings from two
    different vector spaces. Omitted by direct/legacy callers (tests, the
    reserved TTS profile) that manage the store by hand.
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
    if model_id:
        store["model_id"] = model_id

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
    try:
        model_id = resolve_speaker_model_id(cfg)
    except Exception:
        model_id = None
    return enroll_embedding(name, embedding, path=path, model_id=model_id)


def list_speakers(
    *, cfg: Optional[Dict[str, Any]] = None, path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    mismatch = model_mismatch(cfg, path=path)
    out = []
    for key, entry in sorted((store.get("speakers") or {}).items()):
        if key == RESERVED_TTS_PROFILE_KEY:
            continue  # internal echo-reference profile, never user-facing
        embeddings = entry.get("embeddings") or []
        # Consistency reflects MANUAL samples only -- adaptive (self-learned)
        # embeddings never perturb this UI stat (Part 1.4).
        pairs = [
            cosine_similarity(left, right)
            for i, left in enumerate(embeddings)
            for right in embeddings[i + 1:]
            if left and right and len(left) == len(right)
        ]
        consistency = sum(pairs) / len(pairs) if pairs else None
        # ponytail: three mutually consistent samples are a useful local
        # readiness heuristic; replace with calibrated DET/EER data if this
        # profile ever becomes an authentication boundary.
        ready = len(embeddings) >= MIN_ENROLLMENT_SAMPLES and bool(
            consistency is not None and consistency >= MIN_ENROLLMENT_CONSISTENCY
        )
        out.append(
            {
                "name": entry.get("display_name", key),
                "key": key,
                "is_owner": key == owner_key,
                "embeddings": len(embeddings),
                "adaptive": len(entry.get("adaptive_embeddings") or []),
                "consistency": round(consistency, 3) if consistency is not None else None,
                "samples_needed": max(0, MIN_ENROLLMENT_SAMPLES - len(embeddings)),
                "ready": ready,
                "model_mismatch": mismatch,
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
        store["owner"] = next(
            (k for k in speakers if k != RESERVED_TTS_PROFILE_KEY), None
        )
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


def identify_embedding_details(
    embedding: List[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    path: Optional[Path] = None,
) -> Tuple[str, float, Optional[str]]:
    """Match ``embedding`` against enrolled speakers. Never raises.

    Also returns the enrolled display name for a match, or ``None`` when
    nothing clears ``threshold``.
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
        return UNKNOWN_LABEL, max(best_score, 0.0), None
    entry = speakers[best_key]
    return (
        OWNER_LABEL if best_key == owner_key else GUEST_LABEL,
        best_score,
        str(entry.get("display_name") or best_key),
    )


def identify_embedding(
    embedding: List[float],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    path: Optional[Path] = None,
) -> Tuple[str, float]:
    label, score, _name = identify_embedding_details(embedding, threshold=threshold, path=path)
    return label, score


def identify_details(
    pcm16_bytes_16k: bytes,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Tuple[str, float, Optional[str]]:
    """Compute an embedding for ``pcm16_bytes_16k`` and identify the speaker.

    Never raises: any problem (no store, no model, bad audio) degrades to
    ``("unknown", 0.0, None)``.
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
            return UNKNOWN_LABEL, 0.0, None
        embedding = compute_embedding(pcm16_bytes_16k, cfg=cfg_dict)
        if embedding is None:
            return UNKNOWN_LABEL, 0.0, None
        return identify_embedding_details(embedding, threshold=threshold, path=store_path)
    except Exception:
        logger.exception("Speaker identify failed; returning unknown")
        return UNKNOWN_LABEL, 0.0, None


def identify(
    pcm16_bytes_16k: bytes,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Tuple[str, float]:
    label, score, _name = identify_details(pcm16_bytes_16k, cfg=cfg, path=path)
    return label, score


DEFAULT_FOCUS_MODE = "owner"


def focus_mode_setting(cfg: Optional[Dict[str, Any]] = None) -> str:
    """``voice.speaker_id.focus_mode`` -- "owner" (default) or "off".

    Speaker ID is voice FOCUS, not access control (v1 repurpose, see the
    duplex spec doc's speaker-ID section): when "owner", the duplex
    session filters non-owner utterances out of the conversation (never
    reach the instant lane, never get TTS, never enter the rolling
    transcript) and requires an owner-voice match before honoring
    barge-in. "off" restores plain VAD-only/no-filtering behavior for
    every speaker. Any other value falls back to the default rather than
    erroring.
    """
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    value = (
        str(cfg_get(cfg, "voice", "speaker_id", "focus_mode", default=DEFAULT_FOCUS_MODE) or DEFAULT_FOCUS_MODE)
        .strip()
        .lower()
    )
    return value if value in ("owner", "off") else DEFAULT_FOCUS_MODE


def focus_mode_ready(cfg: Optional[Dict[str, Any]] = None, *, path: Optional[Path] = None) -> bool:
    """True when there's an enrolled owner AND the embedding model loads.

    Both conditions must hold before focus mode does anything active --
    callers must otherwise behave exactly as they did before this feature
    existed (never filter/block an un-enrolled user's voice, or a fresh
    install with no speaker model cached yet).
    """
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    if not owner_key:
        return False
    owner_entry = (store.get("speakers") or {}).get(owner_key)
    if not owner_entry or not owner_entry.get("embeddings"):
        return False
    try:
        model_path = resolve_speaker_model_path(cfg)
        _get_extractor(model_path)
    except Exception:
        return False
    return True


def focus_mode_active(cfg: Optional[Dict[str, Any]] = None, *, path: Optional[Path] = None) -> bool:
    """Whether voice focus should currently filter/gate for the owner.

    Combines the ``voice.speaker_id.focus_mode`` setting with real-world
    readiness (:func:`focus_mode_ready`). Backs both the duplex session's
    utterance filtering and its barge-in owner-confirmation gate, so the
    two features turn on/off together.
    """
    if focus_mode_setting(cfg) == "off":
        return False
    return focus_mode_ready(cfg, path=path)


# ---------------------------------------------------------------------------
# Fail-open voice-focus redesign (2026-07-15 round): three-zone identify,
# self-adaptation, TTS self-echo reference, and model-mismatch safety.
#
# Root cause this replaces: a single threshold turned "uncertain" into
# "drop" (fail-closed). Live logs showed real owner utterances scoring
# 0.22-0.33 (far-field vs. the 10 close-mic enrollment samples) getting
# silently dropped, and legitimate barge-in suppressed almost always because
# it required a POSITIVE owner match before interrupting playback at all.
# The zones below make "uncertain" fail OPEN by default and only drop when
# there's actual contrary evidence (a confidently different voice, heard
# recently) -- see identify_zoned()'s docstring and the duplex spec's §4.
# ---------------------------------------------------------------------------

ZONE_OWNER = "OWNER"
ZONE_CONFIDENT_OTHER = "CONFIDENT_OTHER"
ZONE_ABSTAIN = "ABSTAIN"

DEFAULT_REJECT_THRESHOLD = 0.25
DEFAULT_CONTINUITY_SECONDS = 120.0
DEFAULT_COMPETING_WINDOW_SECONDS = 90.0
# Fixed (not config-exposed): below this much clean audio, a low score is
# just as likely to be "not enough signal" as "a different voice" -- so a
# short utterance can never land in CONFIDENT_OTHER, only ABSTAIN.
MIN_CONFIDENT_OTHER_AUDIO_MS = 2000.0

# Self-adaptation ring cap (Part 1.4) -- FIFO, oldest evicted first.
ADAPTIVE_RING_CAP = 20


def reject_threshold(cfg: Optional[Dict[str, Any]] = None) -> float:
    """``voice.speaker_id.reject_threshold`` -- at/below this cosine score
    (and given enough clean audio, see :data:`MIN_CONFIDENT_OTHER_AUDIO_MS`)
    a candidate is confidently NOT the owner (zone CONFIDENT_OTHER)."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    try:
        return float(
            cfg_get(cfg, "voice", "speaker_id", "reject_threshold", default=DEFAULT_REJECT_THRESHOLD)
        )
    except (TypeError, ValueError):
        return DEFAULT_REJECT_THRESHOLD


def continuity_seconds(cfg: Optional[Dict[str, Any]] = None) -> float:
    """``voice.speaker_id.continuity_seconds`` -- how long an ABSTAIN keeps
    resolving to owner-accept after the last owner-labeled utterance."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    try:
        return float(
            cfg_get(cfg, "voice", "speaker_id", "continuity_seconds", default=DEFAULT_CONTINUITY_SECONDS)
        )
    except (TypeError, ValueError):
        return DEFAULT_CONTINUITY_SECONDS


def competing_window_seconds(cfg: Optional[Dict[str, Any]] = None) -> float:
    """``voice.speaker_id.competing_window_seconds`` -- how long the
    per-session "another voice was just heard" flag stays armed after a
    CONFIDENT_OTHER utterance. ABSTAIN only drops while this flag is armed
    (see the duplex session's ``_other_voice_active``)."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    try:
        return float(
            cfg_get(
                cfg, "voice", "speaker_id", "competing_window_seconds",
                default=DEFAULT_COMPETING_WINDOW_SECONDS,
            )
        )
    except (TypeError, ValueError):
        return DEFAULT_COMPETING_WINDOW_SECONDS


def owner_threshold(cfg: Optional[Dict[str, Any]] = None) -> float:
    """``voice.speaker_id.threshold`` -- at/above this cosine score a
    candidate IS the owner (zone OWNER)."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    try:
        return float(cfg_get(cfg, "voice", "speaker_id", "threshold", default=DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def _entry_embeddings(entry: Dict[str, Any]) -> List[List[float]]:
    manual = entry.get("embeddings") or []
    adaptive = entry.get("adaptive_embeddings") or []
    return [e for e in (list(manual) + list(adaptive)) if e]


def _speaker_score(entry: Dict[str, Any], embedding: List[float]) -> float:
    """Max cosine similarity of ``embedding`` against every manual + adaptive
    sample on ``entry`` (max-sim, not average).

    Chosen over averaging deliberately: the adaptive ring self-populates
    from live far-field audio with no human review, so a handful of noisy
    adaptive samples pulling the centroid away from the clean manual
    enrollment average would make the profile WORSE over time. Max-sim lets
    any single strong match (manual or adaptive) carry the score, so a noisy
    adaptive sample can only ever help (find an extra angle that matches) and
    never drag a good manual match down. This is the "better-tested" choice
    called for in the spec: it degrades gracefully as adaptive samples
    accumulate, where averaging degrades unpredictably.
    """
    vectors = _entry_embeddings(entry)
    if not vectors:
        return -1.0
    return max(cosine_similarity(embedding, v) for v in vectors)


def append_adaptive_embedding(
    embedding: List[float], *, path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Append ``embedding`` to the OWNER's adaptive ring (Part 1.4 self-
    adaptation). FIFO-capped at :data:`ADAPTIVE_RING_CAP`; stored in a
    separate ``adaptive_embeddings`` list so it never touches the manual
    ``embeddings`` list the UI consistency stat is computed from. No-op
    (returns the store unchanged) when no owner is enrolled yet.
    """
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    if not owner_key or not embedding:
        return store
    speakers: Dict[str, Any] = store["speakers"]
    entry = speakers.setdefault(owner_key, {"display_name": owner_key, "embeddings": []})
    ring: List[List[float]] = entry.setdefault("adaptive_embeddings", [])
    ring.append([float(x) for x in embedding])
    if len(ring) > ADAPTIVE_RING_CAP:
        del ring[: len(ring) - ADAPTIVE_RING_CAP]
    _atomic_write_json(path, store)
    return store


def reset_adaptive(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """Clear the owner's adaptive ring (``hermes voice speakers
    --reset-adaptive``). Manual enrollment samples are untouched."""
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    speakers = store.get("speakers") or {}
    if owner_key and owner_key in speakers:
        speakers[owner_key]["adaptive_embeddings"] = []
        _atomic_write_json(path, store)
    return store


def adaptive_count(*, path: Optional[Path] = None) -> int:
    path = path or default_store_path()
    store = load_store(path)
    owner_key = store.get("owner")
    if not owner_key:
        return 0
    entry = (store.get("speakers") or {}).get(owner_key) or {}
    return len(entry.get("adaptive_embeddings") or [])


def model_mismatch(cfg: Optional[Dict[str, Any]] = None, *, path: Optional[Path] = None) -> bool:
    """True when the store's embeddings were captured under a different
    ``voice.speaker_id.model`` than what's currently configured.

    Embeddings from two different models live in different, incomparable
    vector spaces -- comparing across them is not just less accurate, it's
    meaningless (cosine similarity between unrelated spaces is noise dressed
    up as a number). Callers (identify_zoned, the CLI, the settings UI) must
    treat a mismatch as "re-enroll needed", never silently keep matching.

    ``False`` when the store has no speakers yet (nothing to mismatch) or no
    ``model_id`` was ever recorded (pre-registry stores -- treated as having
    been captured with :data:`DEFAULT_SPEAKER_MODEL_ID`, which is what
    actually computed them before this registry existed).
    """
    path = path or default_store_path()
    store = load_store(path)
    speakers = store.get("speakers") or {}
    # The reserved TTS profile doesn't count -- it's re-synthesized whenever
    # its own fingerprint goes stale (see hermes_cli.web_server's TTS
    # self-enrollment), never left stranded on an old model.
    if not any(k != RESERVED_TTS_PROFILE_KEY for k in speakers):
        return False
    stored_model_id = store.get("model_id") or DEFAULT_SPEAKER_MODEL_ID
    try:
        current_model_id = resolve_speaker_model_id(cfg)
    except Exception:
        return False
    return stored_model_id != current_model_id


def _audio_ms(pcm16_bytes_16k: bytes) -> int:
    if not pcm16_bytes_16k:
        return 0
    return int(len(pcm16_bytes_16k) / 2 / 16000 * 1000)


def identify_zoned(
    pcm16_bytes_16k: bytes,
    *,
    cfg: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Three-zone speaker identification for voice FOCUS (fail-open design).

    Returns a dict: ``{"zone": OWNER|CONFIDENT_OTHER|ABSTAIN, "label":
    "owner"|"guest"|"unknown", "score": float, "audio_ms": int, "name":
    Optional[str], "model_mismatch": bool, "embedding": Optional[list]}``.

    Zone rules (against the OWNER profile's score; see
    :func:`_speaker_score` for how manual + adaptive samples combine):

    - **OWNER**: owner score >= :func:`owner_threshold`. label="owner".
    - **OWNER** (enrolled-non-owner acceptance): no owner match, but some
      OTHER enrolled speaker's score >= :func:`owner_threshold`. Treated
      like an OWNER-zone acceptance for focus purposes (never dropped) but
      keeps its own badge: label="guest".
    - **CONFIDENT_OTHER**: owner score <= :func:`reject_threshold` AND at
      least :data:`MIN_CONFIDENT_OTHER_AUDIO_MS` of clean audio. A
      confidently-different voice -- label="unknown" (not matched to any
      enrolled guest profile; that case is the OWNER/guest branch above).
    - **ABSTAIN**: everything else -- score between the two thresholds, OR
      audio too short/degraded (embedding computation failed), OR no owner
      enrolled at all. The duplex session (not this function) decides
      ABSTAIN's fate via continuity/competing-flag context -- see
      ``hermes_cli.web_server._DuplexSession._finalize_utterance``.

    Never raises: any problem (no store, no model, bad/short audio, model
    mismatch) degrades to ABSTAIN/unknown, consistent with every other
    function in this module's "can't identify right now, not a hard error"
    contract.
    """
    audio_ms = _audio_ms(pcm16_bytes_16k)
    abstain = {
        "zone": ZONE_ABSTAIN, "label": UNKNOWN_LABEL, "score": 0.0,
        "audio_ms": audio_ms, "name": None, "model_mismatch": False, "embedding": None,
    }

    try:
        try:
            from hermes_cli.config import load_config

            cfg_dict = cfg if cfg is not None else load_config()
        except Exception:
            cfg_dict = cfg

        store_path = path or default_store_path()
        if not store_path.exists():
            return abstain
        if model_mismatch(cfg_dict, path=store_path):
            return {**abstain, "model_mismatch": True}

        embedding = compute_embedding(pcm16_bytes_16k, cfg=cfg_dict)
        if embedding is None:
            return abstain

        store = load_store(store_path)
        owner_key = store.get("owner")
        speakers = store.get("speakers") or {}
        if not owner_key or owner_key not in speakers:
            return {**abstain, "embedding": embedding}

        owner_thr = owner_threshold(cfg_dict)
        reject_thr = reject_threshold(cfg_dict)

        owner_score = _speaker_score(speakers[owner_key], embedding)

        best_other_key: Optional[str] = None
        best_other_score = -1.0
        for key, entry in speakers.items():
            if key in (owner_key, RESERVED_TTS_PROFILE_KEY):
                continue
            score = _speaker_score(entry, embedding)
            if score > best_other_score:
                best_other_key, best_other_score = key, score

        if owner_score >= owner_thr:
            return {
                "zone": ZONE_OWNER, "label": OWNER_LABEL, "score": owner_score,
                "audio_ms": audio_ms, "name": speakers[owner_key].get("display_name"),
                "model_mismatch": False, "embedding": embedding,
            }
        if best_other_key is not None and best_other_score >= owner_thr:
            return {
                "zone": ZONE_OWNER, "label": GUEST_LABEL, "score": best_other_score,
                "audio_ms": audio_ms, "name": speakers[best_other_key].get("display_name"),
                "model_mismatch": False, "embedding": embedding,
            }
        if owner_score <= reject_thr and audio_ms >= MIN_CONFIDENT_OTHER_AUDIO_MS:
            return {
                "zone": ZONE_CONFIDENT_OTHER, "label": UNKNOWN_LABEL, "score": owner_score,
                "audio_ms": audio_ms, "name": None, "model_mismatch": False, "embedding": embedding,
            }
        return {
            "zone": ZONE_ABSTAIN, "label": UNKNOWN_LABEL, "score": owner_score,
            "audio_ms": audio_ms, "name": None, "model_mismatch": False, "embedding": embedding,
        }
    except Exception:
        logger.exception("Speaker zoned-identify failed; returning ABSTAIN")
        return abstain


# ---------------------------------------------------------------------------
# Reserved TTS self-voice profile (Part 1.3: barge-in confirm-negative).
# Cached like any other speaker profile but under a reserved key that
# list_speakers() filters out and enroll_embedding()'s owner-promotion logic
# never sees (this store is written directly, not through enroll_embedding).
# ---------------------------------------------------------------------------


def store_tts_profile(
    embeddings: List[List[float]], *, fingerprint: str = "", path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist Marvi's own synthesized-voice embeddings under the reserved
    ``__marvi_tts__`` key. ``fingerprint`` is an opaque caller-computed
    string (see ``hermes_cli.web_server``'s TTS-config fingerprint) letting
    the caller cheaply detect "the TTS voice config changed, re-synthesize"
    without needing to know anything about TTS itself here.
    """
    path = path or default_store_path()
    store = load_store(path)
    store["speakers"][RESERVED_TTS_PROFILE_KEY] = {
        "display_name": RESERVED_TTS_PROFILE_KEY,
        "embeddings": [[float(x) for x in e] for e in embeddings if e],
        "reserved": True,
        "tts_fingerprint": fingerprint,
    }
    _atomic_write_json(path, store)
    return store


def stored_tts_fingerprint(*, path: Optional[Path] = None) -> Optional[str]:
    """The fingerprint the cached TTS profile was captured under, or
    ``None`` when nothing is cached yet (forces a first-time synthesize)."""
    path = path or default_store_path()
    store = load_store(path)
    entry = (store.get("speakers") or {}).get(RESERVED_TTS_PROFILE_KEY)
    if not entry or not entry.get("embeddings"):
        return None
    return entry.get("tts_fingerprint")


def tts_echo_score(embedding: List[float], *, path: Optional[Path] = None) -> float:
    """Cosine similarity of ``embedding`` against the cached TTS self-voice
    profile, or ``-1.0`` when nothing is enrolled yet (never matches)."""
    path = path or default_store_path()
    store = load_store(path)
    entry = (store.get("speakers") or {}).get(RESERVED_TTS_PROFILE_KEY)
    if not entry:
        return -1.0
    return _speaker_score(entry, embedding)
