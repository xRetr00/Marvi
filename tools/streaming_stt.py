"""Local streaming speech-to-text helpers for the desktop voice loop.

The stable batch STT endpoint remains in ``tools.transcription_tools``.  This
module is intentionally opt-in and dependency-light: sherpa-onnx is imported
only when a streaming session is actually started.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_dir

logger = logging.getLogger(__name__)


DEFAULT_SHERPA_MODEL_ID = "en-20m-int8"
_SHERPA_EN_20M_REPO = "csukuangfj/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17"
_SHERPA_EN_20M_FILES = {
    "encoder": "encoder-epoch-99-avg-1.int8.onnx",
    "decoder": "decoder-epoch-99-avg-1.int8.onnx",
    "joiner": "joiner-epoch-99-avg-1.int8.onnx",
    "tokens": "tokens.txt",
}


@dataclass(frozen=True)
class StreamingSttConfig:
    enabled: bool = False
    provider: str = "sherpa_onnx"
    model: str = DEFAULT_SHERPA_MODEL_ID
    sample_rate: int = 16000
    frame_ms: int = 100
    endpoint_silence_ms: int = 1200
    partial_interval_ms: int = 150


class StreamingSttUnavailable(RuntimeError):
    """Raised when opt-in streaming STT cannot start."""


def _positive_int(value: Any, default: int, *, min_value: int = 1, max_value: int = 60_000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if min_value <= parsed <= max_value else default


def streaming_stt_config(config: Optional[dict[str, Any]] = None) -> StreamingSttConfig:
    stt = (config or {}).get("stt") if isinstance(config, dict) else {}
    stt = stt if isinstance(stt, dict) else {}
    raw = stt.get("streaming")
    raw = raw if isinstance(raw, dict) else {}

    return StreamingSttConfig(
        enabled=raw.get("enabled") is True,
        provider=str(raw.get("provider") or "sherpa_onnx").strip().lower() or "sherpa_onnx",
        model=str(raw.get("model") or DEFAULT_SHERPA_MODEL_ID).strip() or DEFAULT_SHERPA_MODEL_ID,
        sample_rate=_positive_int(raw.get("sample_rate"), 16000, min_value=8000, max_value=48000),
        frame_ms=_positive_int(raw.get("frame_ms"), 100, min_value=20, max_value=500),
        endpoint_silence_ms=_positive_int(raw.get("endpoint_silence_ms"), 1200, min_value=100, max_value=10000),
        partial_interval_ms=_positive_int(raw.get("partial_interval_ms"), 150, min_value=50, max_value=2000),
    )


def _import_sherpa_onnx():
    try:
        import sherpa_onnx  # type: ignore
    except ImportError as exc:
        raise StreamingSttUnavailable(
            "sherpa-onnx is not installed. Run `hermes tools post-setup sherpa_onnx` "
            "or install it with `pip install sherpa-onnx`. Keep stt.streaming.enabled "
            "false to use batch faster-whisper."
        ) from exc
    return sherpa_onnx


def _model_cache_dir(model_id: str) -> Path:
    return Path(get_hermes_dir(f"cache/sherpa-onnx/{model_id}", "sherpa_onnx_cache"))


def _download_hf_file(repo: str, filename: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{repo}/resolve/main/{filename}"
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as out:
            out.write(response.read())
        tmp.replace(target)
    except (OSError, urllib.error.URLError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise StreamingSttUnavailable(f"Could not download sherpa-onnx model file {filename}: {exc}") from exc


def resolve_sherpa_model_files(cfg: StreamingSttConfig) -> dict[str, str]:
    model_value = cfg.model.strip()
    model_path = Path(model_value).expanduser()

    if model_path.exists():
        root = model_path
        files = {
            "encoder": next(root.glob("encoder*.onnx"), None),
            "decoder": next(root.glob("decoder*.onnx"), None),
            "joiner": next(root.glob("joiner*.onnx"), None),
            "tokens": root / "tokens.txt",
        }
    elif model_value == DEFAULT_SHERPA_MODEL_ID:
        root = _model_cache_dir(DEFAULT_SHERPA_MODEL_ID)
        for filename in _SHERPA_EN_20M_FILES.values():
            target = root / filename
            if not target.exists():
                logger.info("[StreamingSTT] Downloading %s", filename)
                _download_hf_file(_SHERPA_EN_20M_REPO, filename, target)
        files = {key: root / filename for key, filename in _SHERPA_EN_20M_FILES.items()}
    else:
        raise StreamingSttUnavailable(
            f"Unknown streaming STT model {cfg.model!r}. Use {DEFAULT_SHERPA_MODEL_ID!r} "
            "or set stt.streaming.model to a local sherpa-onnx model directory."
        )

    missing = [key for key, value in files.items() if not value or not Path(value).exists()]
    if missing:
        raise StreamingSttUnavailable(f"Sherpa streaming model is missing files: {', '.join(missing)}")

    return {key: str(value) for key, value in files.items()}


class SherpaOnnxStreamingRecognizer:
    def __init__(self, cfg: StreamingSttConfig):
        self.cfg = cfg
        sherpa_onnx = _import_sherpa_onnx()
        files = resolve_sherpa_model_files(cfg)
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=files["encoder"],
            decoder=files["decoder"],
            joiner=files["joiner"],
            tokens=files["tokens"],
            num_threads=2,
            sample_rate=cfg.sample_rate,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )
        self.stream = self.recognizer.create_stream()
        self.sample_rate = cfg.sample_rate

    def start(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate or self.cfg.sample_rate

    def accept_waveform(self, samples: list[float]) -> str:
        self.recognizer.accept_waveform(self.stream, self.sample_rate, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        result = self.recognizer.get_result(self.stream)
        return str(getattr(result, "text", result) or "").strip()

    def finish(self) -> str:
        self.recognizer.input_finished(self.stream)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        result = self.recognizer.get_result(self.stream)
        return str(getattr(result, "text", result) or "").strip()


class StreamingSttFactory:
    def __init__(self, create_recognizer: Optional[Callable[[StreamingSttConfig], Any]] = None):
        self._create_recognizer = create_recognizer or (lambda cfg: SherpaOnnxStreamingRecognizer(cfg))

    def create(self, config: Optional[dict[str, Any]] = None):
        cfg = streaming_stt_config(config)
        if not cfg.enabled:
            raise StreamingSttUnavailable("Streaming STT is disabled in stt.streaming.enabled")
        if cfg.provider != "sherpa_onnx":
            raise StreamingSttUnavailable(f"Unsupported streaming STT provider: {cfg.provider}")
        return self._create_recognizer(cfg)
