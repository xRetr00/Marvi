"""Local streaming speech-to-text helpers for the desktop voice loop.

The stable batch STT endpoint remains in ``tools.transcription_tools``.  This
module is intentionally opt-in and dependency-light: sherpa-onnx is imported
only when a streaming session is actually started.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
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

DEFAULT_WAKE_WORD_MODEL_ID = "kws-en-3.3m"
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


@dataclass(frozen=True)
class StreamingSttConfig:
    enabled: bool = False
    provider: str = "sherpa_onnx"
    model: str = DEFAULT_SHERPA_MODEL_ID
    sample_rate: int = 16000
    frame_ms: int = 100
    endpoint_silence_ms: int = 1200
    partial_interval_ms: int = 150


@dataclass(frozen=True)
class WakeWordConfig:
    enabled: bool = False
    provider: str = "sherpa_onnx"
    model: str = DEFAULT_WAKE_WORD_MODEL_ID
    sample_rate: int = 16000
    phrases: tuple[str, ...] = DEFAULT_WAKE_WORD_PHRASES
    boost: float = 2.0
    threshold: float = 0.35
    command_timeout_ms: int = 8000
    cooldown_ms: int = 1200


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


def wake_word_config(config: Optional[dict[str, Any]] = None) -> WakeWordConfig:
    voice = (config or {}).get("voice") if isinstance(config, dict) else {}
    voice = voice if isinstance(voice, dict) else {}
    raw = voice.get("wake_word")
    raw = raw if isinstance(raw, dict) else {}

    return WakeWordConfig(
        enabled=raw.get("enabled") is True,
        provider=str(raw.get("provider") or "sherpa_onnx").strip().lower() or "sherpa_onnx",
        model=str(raw.get("model") or DEFAULT_WAKE_WORD_MODEL_ID).strip() or DEFAULT_WAKE_WORD_MODEL_ID,
        sample_rate=_positive_int(raw.get("sample_rate"), 16000, min_value=8000, max_value=48000),
        phrases=_normalize_phrases(raw.get("phrases")),
        boost=_float_value(raw.get("boost"), 2.0, min_value=0.1, max_value=10.0),
        threshold=_float_value(raw.get("threshold"), 0.35, min_value=0.05, max_value=0.95),
        command_timeout_ms=_positive_int(raw.get("command_timeout_ms"), 8000, min_value=1000, max_value=30000),
        cooldown_ms=_positive_int(raw.get("cooldown_ms"), 1200, min_value=0, max_value=10000),
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
        raise StreamingSttUnavailable(f"Could not download sherpa-onnx wake-word model: {exc}") from exc


def _extract_tar_bz2(archive: Path, target_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="hermes-kws-") as tmp_name:
        tmp_dir = Path(tmp_name)
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                root = tmp_dir.resolve()
                for member in tar.getmembers():
                    destination = (root / member.name).resolve()
                    if root not in destination.parents and destination != root:
                        raise StreamingSttUnavailable("Wake-word model archive contains an unsafe path")
                tar.extractall(tmp_dir)
        except (tarfile.TarError, OSError) as exc:
            raise StreamingSttUnavailable(f"Could not extract sherpa-onnx wake-word model: {exc}") from exc

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
        raise StreamingSttUnavailable(
            f"Unknown wake-word model {cfg.model!r}. Use {DEFAULT_WAKE_WORD_MODEL_ID!r} "
            "or set voice.wake_word.model to a local sherpa-onnx KWS model directory."
        )

    files = {
        "encoder": next(root.glob("encoder*.onnx"), None),
        "decoder": next(root.glob("decoder*.onnx"), None),
        "joiner": next(root.glob("joiner*.onnx"), None),
        "tokens": root / "tokens.txt",
        "bpe_model": root / "bpe.model",
    }
    missing = [key for key, value in files.items() if not value or not Path(value).exists()]
    if missing:
        raise StreamingSttUnavailable(f"Sherpa wake-word model is missing files: {', '.join(missing)}")

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
        raise StreamingSttUnavailable(
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StreamingSttUnavailable(f"Could not tokenize wake-word phrases: {exc}") from exc

    if result.returncode != 0:
        raise StreamingSttUnavailable(
            "Could not tokenize wake-word phrases with sherpa-onnx-cli: "
            f"{(result.stderr or result.stdout or '').strip()[:300]}"
        )

    return str(target)


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
            provider="cpu",
        )
        self.stream = self.spotter.create_stream()
        self.sample_rate = cfg.sample_rate

    def start(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate or self.cfg.sample_rate

    def accept_waveform(self, samples: list[float]) -> str:
        self.stream.accept_waveform(self.sample_rate, samples)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        result = str(self.spotter.get_result(self.stream) or "").strip()
        if result:
            self.spotter.reset_stream(self.stream)
        return result.replace("_", " ")

    def stop(self) -> None:
        try:
            self.stream.input_finished()
        except Exception:
            pass


class WakeWordFactory:
    def __init__(self, create_spotter: Optional[Callable[[WakeWordConfig], Any]] = None):
        self._create_spotter = create_spotter or (lambda cfg: SherpaOnnxWakeWordSpotter(cfg))

    def create(self, config: Optional[dict[str, Any]] = None):
        cfg = wake_word_config(config)
        if not cfg.enabled:
            raise StreamingSttUnavailable("Wake word is disabled in voice.wake_word.enabled")
        if cfg.provider != "sherpa_onnx":
            raise StreamingSttUnavailable(f"Unsupported wake-word provider: {cfg.provider}")
        return self._create_spotter(cfg)
