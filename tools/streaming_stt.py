"""Local wake-word helpers for the desktop voice loop.

The speech-to-text path remains the stable batch endpoint in
``tools.transcription_tools``. Marvi wake-word detection uses LiveKit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import threading
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_dir, get_hermes_home

logger = logging.getLogger(__name__)


DEFAULT_WAKE_WORD_MODEL_ID = "kws-en-3.3m"
DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID = "livekit-marvi"
DEFAULT_WAKE_WORD_PHRASES = (
    "hey marvi",
    "hi marvi",
    "okay marvi",
    "ok marvi",
    "yo marvi",
    "marvi",
    "hey marve",
    "hey marvy",
    "hey marvie",
    "hey marfi",
    "hey marfe",
    "hey marvey",
    "marve",
    "marvy",
    "marvie",
    "marfi",
    "marfe",
    "marvey",
)
_SHERPA_KWS_EN_REPO_ARCHIVE = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
    "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2"
)
_SHERPA_NATIVE_PROBE_TIMEOUT_SECONDS = 10
_WAKEWORD_TELEMETRY_LOG = "wakeword-livekit.jsonl"
_WAKEWORD_TELEMETRY_MAX_BYTES = 8 * 1024 * 1024
_WAKEWORD_TELEMETRY_BACKUPS = 2
_WAKEWORD_DEBUG_SAMPLE_SECONDS = 5.0
_WAKEWORD_TELEMETRY_LOCK = threading.Lock()


def _append_wakeword_telemetry(log_path: Path, line: str) -> None:
    """Append one bounded JSONL event, rotating before debug logs grow forever."""
    encoded_size = len(line.encode("utf-8")) + 1
    with _WAKEWORD_TELEMETRY_LOCK:
        try:
            current_size = log_path.stat().st_size
        except OSError:
            current_size = 0
        if current_size and current_size + encoded_size > _WAKEWORD_TELEMETRY_MAX_BYTES:
            # A legacy unbounded debug log can already be hundreds of MB. Do
            # not preserve that oversized file as a backup; start the bounded
            # set immediately on the next event.
            if current_size > _WAKEWORD_TELEMETRY_MAX_BYTES:
                try:
                    log_path.unlink()
                except FileNotFoundError:
                    pass
                current_size = 0
            else:
                oldest = log_path.with_name(f"{log_path.name}.{_WAKEWORD_TELEMETRY_BACKUPS}")
                try:
                    oldest.unlink(missing_ok=True)
                except OSError:
                    pass
                for index in range(_WAKEWORD_TELEMETRY_BACKUPS - 1, 0, -1):
                    source = log_path.with_name(f"{log_path.name}.{index}")
                    target = log_path.with_name(f"{log_path.name}.{index + 1}")
                    try:
                        source.replace(target)
                    except FileNotFoundError:
                        pass
                try:
                    log_path.replace(log_path.with_name(f"{log_path.name}.1"))
                except FileNotFoundError:
                    pass
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool = False
    provider: str = "livekit"
    model: str = DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID
    sample_rate: int = 16000
    phrases: tuple[str, ...] = DEFAULT_WAKE_WORD_PHRASES
    boost: float = 2.0
    threshold: float = 0.35
    debug: bool = False
    command_timeout_ms: int = 8000
    cooldown_ms: int = 1200
    # Reject wake detections whose audio window is essentially silence. The
    # LiveKit model scores ~0.79 for "marvi" on a silent room (rms ~0.001), which
    # caused constant false wakes when the room was empty; real speech is rms
    # ~0.04 (40x louder), so this cleanly separates them. Tune via
    # voice.wake_word.min_rms (0 disables).
    min_rms: float = 0.01


class WakeWordUnavailable(RuntimeError):
    """Raised when local wake-word detection cannot start."""


def _positive_int(value: Any, default: int, *, min_value: int = 1, max_value: int = 60_000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if min_value <= parsed <= max_value else default


def _float_value(value: Any, default: float, *, min_value: float, max_value: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if min_value <= parsed <= max_value else default


def _normalize_phrase(value: Any) -> str:
    text = str(value or "").strip().lower()
    return " ".join(text.split())


def _normalize_phrases(value: Any) -> tuple[str, ...]:
    raw_items = value if isinstance(value, list) else DEFAULT_WAKE_WORD_PHRASES
    phrases: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        phrase = _normalize_phrase(item)
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        phrases.append(phrase)
    return tuple(phrases) if phrases else DEFAULT_WAKE_WORD_PHRASES


def wake_word_config(config: Optional[dict[str, Any]] = None) -> WakeWordConfig:
    voice = (config or {}).get("voice") if isinstance(config, dict) else {}
    voice = voice if isinstance(voice, dict) else {}
    raw = voice.get("wake_word")
    raw = raw if isinstance(raw, dict) else {}
    # Marvi uses LiveKit exclusively for wake-word detection. Sherpa ONNX is
    # still used independently by speaker identification.
    provider = "livekit"
    default_model = DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID
    model = str(raw.get("model") or default_model).strip() or default_model
    if model == DEFAULT_WAKE_WORD_MODEL_ID:
        model = default_model

    return WakeWordConfig(
        enabled=raw.get("enabled") is True,
        provider=provider,
        model=model,
        sample_rate=_positive_int(raw.get("sample_rate"), 16000, min_value=8000, max_value=48000),
        phrases=_normalize_phrases(raw.get("phrases")),
        boost=_float_value(raw.get("boost"), 2.0, min_value=0.1, max_value=10.0),
        threshold=_float_value(raw.get("threshold"), 0.35, min_value=0.05, max_value=0.95),
        min_rms=_float_value(raw.get("min_rms"), 0.01, min_value=0.0, max_value=1.0),
        debug=raw.get("debug") is True,
        command_timeout_ms=_positive_int(raw.get("command_timeout_ms"), 8000, min_value=1000, max_value=30000),
        cooldown_ms=_positive_int(raw.get("cooldown_ms"), 1200, min_value=0, max_value=10000),
    )


def _import_sherpa_onnx():
    try:
        import sherpa_onnx  # type: ignore
    except ImportError as exc:
        raise WakeWordUnavailable(
            "sherpa-onnx is not installed. Run `hermes tools post-setup sherpa_onnx` "
            "or install it with `pip install sherpa-onnx` to enable wake word."
        ) from exc
    return sherpa_onnx


def _import_livekit_wakeword_model():
    try:
        from livekit.wakeword import WakeWordModel  # type: ignore
    except ImportError as exc:
        raise WakeWordUnavailable(
            "livekit-wakeword is not installed. Run `hermes tools post-setup livekit_wakeword` "
            "or install it with `pip install livekit-wakeword` to enable LiveKit wake word."
        ) from exc
    return WakeWordModel


def _model_cache_dir(model_id: str) -> Path:
    return Path(get_hermes_dir(f"cache/sherpa-onnx/{model_id}", "sherpa_onnx_cache"))


def _livekit_model_cache_dir(model_id: str) -> Path:
    return Path(get_hermes_dir(f"cache/livekit-wakeword/{model_id}", "livekit_wakeword_cache"))


def _download_archive(url: str, target: Path) -> None:
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
        raise WakeWordUnavailable(f"Could not download sherpa-onnx wake-word model: {exc}") from exc


def _extract_tar_bz2(archive: Path, target_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-kws-") as tmp_name:
        tmp_dir = Path(tmp_name)
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                root = tmp_dir.resolve()
                for member in tar.getmembers():
                    destination = (root / member.name).resolve()
                    if root not in destination.parents and destination != root:
                        raise WakeWordUnavailable("Wake-word model archive contains an unsafe path")
                tar.extractall(tmp_dir)
        except (tarfile.TarError, OSError) as exc:
            raise WakeWordUnavailable(f"Could not extract sherpa-onnx wake-word model: {exc}") from exc

        roots = [path for path in tmp_dir.iterdir() if path.is_dir()]
        source = roots[0] if len(roots) == 1 else tmp_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            destination = target_dir / child.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(child), str(destination))


def resolve_sherpa_kws_model_files(cfg: WakeWordConfig) -> dict[str, str]:
    model_value = cfg.model.strip()
    model_path = Path(model_value).expanduser()

    if model_path.exists():
        root = model_path
    elif model_value == DEFAULT_WAKE_WORD_MODEL_ID:
        root = _model_cache_dir(DEFAULT_WAKE_WORD_MODEL_ID)
        if not root.exists() or not any(root.glob("encoder*.onnx")):
            archive = root.with_suffix(".tar.bz2")
            logger.info("[WakeWord] Downloading sherpa-onnx KWS model")
            _download_archive(_SHERPA_KWS_EN_REPO_ARCHIVE, archive)
            _extract_tar_bz2(archive, root)
    else:
        raise WakeWordUnavailable(
            f"Unknown wake-word model {cfg.model!r}. Use {DEFAULT_WAKE_WORD_MODEL_ID!r} "
            "or set voice.wake_word.model to a local sherpa-onnx KWS model directory."
        )

    def preferred_model_file(prefix: str) -> Path | None:
        matches = sorted(root.glob(f"{prefix}*.onnx"))
        return next((path for path in matches if ".int8." not in path.name), None) or (matches[0] if matches else None)

    files = {
        "encoder": preferred_model_file("encoder"),
        "decoder": preferred_model_file("decoder"),
        "joiner": preferred_model_file("joiner"),
        "tokens": root / "tokens.txt",
        "bpe_model": root / "bpe.model",
    }
    missing = [key for key, value in files.items() if not value or not Path(value).exists()]
    if missing:
        raise WakeWordUnavailable(f"Sherpa wake-word model is missing files: {', '.join(missing)}")

    return {key: str(value) for key, value in files.items()}


def _wake_keywords_cache_path(cfg: WakeWordConfig, files: dict[str, str]) -> Path:
    digest_input = "\n".join(cfg.phrases) + f"|{cfg.boost}|{cfg.threshold}|{files['tokens']}|{files['bpe_model']}"
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:12]
    return _model_cache_dir(cfg.model) / f"keywords-{digest}.txt"


def _write_wake_keywords_file(cfg: WakeWordConfig, files: dict[str, str]) -> str:
    target = _wake_keywords_cache_path(cfg, files)
    if target.exists():
        return str(target)

    input_path = target.with_suffix(".input.txt")
    input_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for phrase in cfg.phrases:
        label = phrase.replace(" ", "_")
        lines.append(f"{phrase.upper()} :{cfg.boost:g} #{cfg.threshold:g} @{label}")
    input_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cli = shutil.which("sherpa-onnx-cli")
    if not cli:
        scripts_dir = Path(sys.executable).resolve().parent
        candidates = [
            scripts_dir / "sherpa-onnx-cli.exe",
            scripts_dir / "sherpa-onnx-cli",
            scripts_dir.parent / "Scripts" / "sherpa-onnx-cli.exe",
            scripts_dir.parent / "bin" / "sherpa-onnx-cli",
        ]
        cli = next((str(path) for path in candidates if path.exists()), None)
    if not cli:
        raise WakeWordUnavailable(
            "sherpa-onnx-cli is not available. Re-run `hermes tools post-setup sherpa_onnx` "
            "or ensure the sherpa-onnx scripts directory is on PATH."
        )

    cmd = [
        cli,
        "text2token",
        "--tokens",
        files["tokens"],
        "--tokens-type",
        "bpe",
        "--bpe-model",
        files["bpe_model"],
        str(input_path),
        str(target),
    ]
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WakeWordUnavailable(f"Could not tokenize wake-word phrases: {exc}") from exc

    if result.returncode != 0:
        raise WakeWordUnavailable(
            "Could not tokenize wake-word phrases with sherpa-onnx-cli: "
            f"{(result.stderr or result.stdout or '').strip()[:300]}"
        )

    return str(target)


def prepare_wake_word_assets(config: Optional[dict[str, Any]] = None) -> str:
    cfg = wake_word_config(config)
    cfg = WakeWordConfig(
        enabled=True,
        provider=cfg.provider,
        model=cfg.model,
        sample_rate=cfg.sample_rate,
        phrases=cfg.phrases,
        threshold=cfg.threshold,
        boost=cfg.boost,
        debug=cfg.debug,
        command_timeout_ms=cfg.command_timeout_ms,
        cooldown_ms=cfg.cooldown_ms,
    )
    if cfg.provider != "sherpa_onnx":
        raise WakeWordUnavailable(f"Unsupported wake-word provider: {cfg.provider}")
    files = resolve_sherpa_kws_model_files(cfg)
    return _write_wake_keywords_file(cfg, files)


def _run_sherpa_native_self_test(cfg: WakeWordConfig) -> None:
    cmd = [
        sys.executable,
        "-X",
        "faulthandler",
        "-c",
        (
            "import json, sys; "
            "from tools.streaming_stt import SherpaOnnxWakeWordSpotter, WakeWordConfig; "
            "cfg = WakeWordConfig(**json.load(sys.stdin)); "
            "spotter = SherpaOnnxWakeWordSpotter(cfg); "
            "spotter.stop(); "
            "print('ok')"
        ),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        result = subprocess.run(
            cmd,
            input=json.dumps(asdict(cfg)),
            capture_output=True,
            text=True,
            timeout=_SHERPA_NATIVE_PROBE_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise WakeWordUnavailable("sherpa-onnx native self-test timed out") from exc
    except OSError as exc:
        raise WakeWordUnavailable(f"sherpa-onnx native self-test could not start: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            detail = detail.splitlines()[0][:300]
            raise WakeWordUnavailable(f"sherpa-onnx native self-test failed: {detail}")
        raise WakeWordUnavailable(f"sherpa-onnx native self-test failed with exit code {result.returncode}")


class SherpaOnnxWakeWordSpotter:
    def __init__(self, cfg: WakeWordConfig):
        self.cfg = cfg
        sherpa_onnx = _import_sherpa_onnx()
        files = resolve_sherpa_kws_model_files(cfg)
        keywords_file = _write_wake_keywords_file(cfg, files)
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=files["tokens"],
            encoder=files["encoder"],
            decoder=files["decoder"],
            joiner=files["joiner"],
            num_threads=2,
            keywords_file=keywords_file,
            keywords_score=cfg.boost,
            keywords_threshold=cfg.threshold,
            provider="cpu",
        )
        self.stream = self.spotter.create_stream()
        self.sample_rate = cfg.sample_rate
        self._recent: list[float] = []  # rolling ~1s window for the RMS fallback
        from tools.vad import make_speech_gate

        self._speech_gate = make_speech_gate()

    def start(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate or self.cfg.sample_rate

    def accept_waveform(self, samples: list[float]) -> str:
        if self._speech_gate is not None:
            self._speech_gate.accept(samples)
        elif self.cfg.min_rms > 0:
            self._recent.extend(float(sample) for sample in samples)
            self._recent = self._recent[-16000:]

        self.stream.accept_waveform(self.sample_rate, samples)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        result = str(self.spotter.get_result(self.stream) or "").strip()
        if not result:
            return ""

        self.spotter.reset_stream(self.stream)
        # Speech gate: drop keyword hits fired on silence/noise. Prefer TEN VAD,
        # fall back to the RMS energy gate (see WakeWordConfig).
        if self._speech_gate is not None:
            if not self._speech_gate.has_recent_speech():
                return ""
        elif self.cfg.min_rms > 0:
            import numpy as np

            arr = np.asarray(self._recent, dtype=np.float32)
            rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0
            if rms < self.cfg.min_rms:
                return ""
        return result.replace("_", " ")

    def stop(self) -> None:
        try:
            self.stream.input_finished()
        except Exception:
            pass


def resolve_livekit_wakeword_models(cfg: WakeWordConfig) -> list[Path]:
    model_value = cfg.model.strip()
    model_path = Path(model_value).expanduser()

    if model_path.is_file():
        models = [model_path]
    elif model_path.is_dir():
        models = sorted(model_path.glob("*.onnx"))
    elif model_value == DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID:
        root = _livekit_model_cache_dir(DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID)
        models = sorted(root.glob("*.onnx")) if root.exists() else []
    else:
        raise WakeWordUnavailable(
            f"Unknown LiveKit wake-word model {cfg.model!r}. Set voice.wake_word.model "
            "to a .onnx file or a directory containing hey_marvi/marvi variant .onnx models."
        )

    if not models:
        raise WakeWordUnavailable(
            "LiveKit wake-word model files are missing. Set voice.wake_word.model "
            "to a .onnx file or a directory containing hey_marvi/marvi variant .onnx models."
        )

    return models


class LiveKitWakeWordSpotter:
    def __init__(self, cfg: WakeWordConfig):
        self.cfg = cfg
        WakeWordModel = _import_livekit_wakeword_model()
        self._model_paths = resolve_livekit_wakeword_models(cfg)
        self.model = WakeWordModel(models=self._model_paths)
        self._samples: list[float] = []
        self._frames_seen = 0
        self._last_debug_log_at = 0.0
        # TEN VAD speech gate (falls back to the RMS energy gate if unavailable).
        from tools.vad import make_speech_gate

        self._speech_gate = make_speech_gate()

    def start(self, sample_rate: int = 16000) -> None:
        if sample_rate and sample_rate != 16000:
            logger.warning("[WakeWord] LiveKit wakeword expects 16kHz audio, got %s", sample_rate)

    def accept_waveform(self, samples: list[float]) -> str:
        import numpy as np

        self._frames_seen += 1
        if self._speech_gate is not None:
            self._speech_gate.accept(samples)
        self._samples.extend(float(sample) for sample in samples)
        self._samples = self._samples[-32000:]
        if len(self._samples) < 32000:
            self._log_decision("waiting", samples=samples, window_samples=len(self._samples))
            return ""

        window = np.asarray(self._samples, dtype=np.float32)

        # Speech gate: the model hallucinates a ~0.79 "marvi" score on silence, so
        # reject detections without real speech. Prefer TEN VAD (rejects noisy-but-
        # non-speech too); fall back to an RMS energy gate when it's unavailable.
        if self._speech_gate is not None:
            if not self._speech_gate.has_recent_speech():
                self._log_decision("no_speech", samples=samples, window=window)
                return ""
        elif self.cfg.min_rms > 0:
            rms = float(np.sqrt(np.mean(np.square(window)))) if window.size else 0.0
            if rms < self.cfg.min_rms:
                self._log_decision("low_energy", samples=samples, window=window)
                return ""

        scores = self.model.predict(window)
        if not isinstance(scores, dict) or not scores:
            self._log_decision("empty_scores", samples=samples, window=window)
            return ""
        label, score = max(scores.items(), key=lambda item: float(item[1] or 0))
        passed = float(score or 0) >= self.cfg.threshold
        phrase = str(label).replace("_", " ")
        self._log_decision(
            "passed" if passed else "ignored",
            label=phrase,
            score=float(score or 0),
            scores=scores,
            samples=samples,
            window=window,
        )
        return phrase if passed else ""

    def _log_decision(
        self,
        decision: str,
        *,
        samples: list[float],
        window_samples: int | None = None,
        window: Any = None,
        label: str = "",
        score: float = 0.0,
        scores: dict[str, Any] | None = None,
    ) -> None:
        if not self.cfg.debug:
            return
        # Keep positive detections, but sample routine decisions. Writing each
        # inference frame to two logs made debug mode noticeably stall the
        # local gateway.
        now = time.monotonic()
        if decision != "passed" and now - self._last_debug_log_at < _WAKEWORD_DEBUG_SAMPLE_SECONDS:
            return
        self._last_debug_log_at = now
        try:
            import numpy as np

            arr = np.asarray(window if window is not None else samples, dtype=np.float32).flatten()
            rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0
            peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        except Exception:
            rms = 0.0
            peak = 0.0

        clean_scores = {
            str(key).replace("_", " "): round(float(value or 0), 6)
            for key, value in (scores or {}).items()
        }
        top_scores = dict(sorted(clean_scores.items(), key=lambda item: item[1], reverse=True)[:8])
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": "livekit",
            "decision": decision,
            "label": label,
            "score": round(score, 6),
            "threshold": self.cfg.threshold,
            "model": self.cfg.model,
            "model_files": [path.name for path in self._model_paths],
            "frames_seen": self._frames_seen,
            "frame_samples": len(samples),
            "window_samples": window_samples if window_samples is not None else len(self._samples),
            "rms": round(rms, 6),
            "peak": round(peak, 6),
            "scores": top_scores,
            "phrases": list(self.cfg.phrases),
        }
        log_method = logger.info if decision == "passed" else logger.debug
        log_method(
            "[WakeWord] LiveKit %s label=%s score=%.4f threshold=%.4f rms=%.5f peak=%.5f",
            decision,
            label or "-",
            score,
            self.cfg.threshold,
            rms,
            peak,
        )
        try:
            log_path = get_hermes_home() / "logs" / _WAKEWORD_TELEMETRY_LOG
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _append_wakeword_telemetry(
                log_path, json.dumps(event, separators=(",", ":"))
            )
        except OSError as exc:
            logger.debug("[WakeWord] Could not write LiveKit telemetry: %s", exc)

    def stop(self) -> None:
        close = getattr(self.model, "close", None)
        if callable(close):
            close()


class WakeWordFactory:
    def __init__(
        self,
        create_livekit_spotter: Optional[Callable[[WakeWordConfig], Any]] = None,
    ):
        self._create_livekit_spotter = create_livekit_spotter or (lambda cfg: LiveKitWakeWordSpotter(cfg))

    def create(self, config: Optional[dict[str, Any] | WakeWordConfig] = None):
        cfg = config if isinstance(config, WakeWordConfig) else wake_word_config(config)
        if not cfg.enabled:
            raise WakeWordUnavailable("Wake word is disabled in voice.wake_word.enabled")
        return self._create_livekit_spotter(cfg)


# --- Wake-word warm pool ----------------------------------------------------
# The wake word loads its model lazily on first arm. Warming a spotter at app
# startup (alongside TTS + STT) and handing it to the wake-word stream makes the
# first arm instant. The spotter's detection lifecycle (start/accept/stop) is
# unchanged — this only moves the model load earlier. Additive: the stream falls
# back to a fresh spotter if none is warm.
_WARM_WAKE_LOCK = threading.Lock()
_WARM_WAKE_SPOTTER: Any = None
_WARM_WAKE_SIGNATURE: Any = None


def _wake_signature(config: Optional[dict[str, Any] | WakeWordConfig]) -> Any:
    try:
        cfg = config if isinstance(config, WakeWordConfig) else wake_word_config(config)
        return (cfg.provider, cfg.model, round(float(cfg.threshold), 4), round(float(cfg.boost), 4))
    except Exception:
        return None


def warm_wake_word(config: Optional[dict[str, Any]] = None) -> bool:
    """Preload the wake-word model into the warm pool. Returns False when wake
    word is disabled/unavailable. Safe to call from a background thread."""
    global _WARM_WAKE_SPOTTER, _WARM_WAKE_SIGNATURE
    cfg = wake_word_config(config)
    if not cfg.enabled:
        return False
    signature = _wake_signature(cfg)
    with _WARM_WAKE_LOCK:
        if _WARM_WAKE_SPOTTER is not None and _WARM_WAKE_SIGNATURE == signature:
            return True
    spotter = WakeWordFactory().create(cfg)  # loads the model (the slow part)
    stale = None
    with _WARM_WAKE_LOCK:
        stale = _WARM_WAKE_SPOTTER
        _WARM_WAKE_SPOTTER = spotter
        _WARM_WAKE_SIGNATURE = signature
    if stale is not None:
        try:
            stale.stop()
        except Exception:
            pass
    return True


def take_warm_wake_word_spotter(config: Optional[dict[str, Any]] = None) -> Any:
    """Hand the warm spotter to the wake-word stream, or None if not warm."""
    global _WARM_WAKE_SPOTTER, _WARM_WAKE_SIGNATURE
    signature = _wake_signature(config)
    with _WARM_WAKE_LOCK:
        if _WARM_WAKE_SPOTTER is not None and _WARM_WAKE_SIGNATURE == signature:
            spotter = _WARM_WAKE_SPOTTER
            _WARM_WAKE_SPOTTER = None
            _WARM_WAKE_SIGNATURE = None
            return spotter
    return None
