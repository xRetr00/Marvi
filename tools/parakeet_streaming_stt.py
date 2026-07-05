"""Local Parakeet Realtime EOU speech-to-text for desktop mic audio."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_PARAKEET_MODEL = "nvidia/parakeet_realtime_eou_120m-v1"


@dataclass(frozen=True)
class ParakeetStreamingConfig:
    model: str = DEFAULT_PARAKEET_MODEL
    device: str = "cuda"
    dtype: str = "auto"
    max_gpu_memory_gb: float | None = None
    cpu_fallback: bool = True
    eou_token: str = "<EOU>"


_MODEL_CACHE: dict[tuple[str, str, str, float | None, bool, str], Any] = {}


def resolve_parakeet_config(stt_config: dict[str, Any] | None) -> ParakeetStreamingConfig:
    streaming = (stt_config or {}).get("streaming", {})
    streaming = streaming if isinstance(streaming, dict) else {}
    nested = streaming.get("parakeet", {})
    nested = nested if isinstance(nested, dict) else {}

    def pick(key: str, default: Any) -> Any:
        value = nested.get(key, streaming.get(key, default))
        return default if value in (None, "") else value

    def pick_bool(key: str, default: bool) -> bool:
        value = pick(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    try:
        max_gpu_memory = float(pick("max_gpu_memory_gb", 0) or 0)
    except (TypeError, ValueError):
        max_gpu_memory = 0

    nested_model = nested.get("model")
    legacy_model = streaming.get("model")
    model_value = nested_model
    if model_value in (None, ""):
        legacy_model_text = str(legacy_model or "").strip()
        model_value = legacy_model_text if "parakeet" in legacy_model_text.lower() else DEFAULT_PARAKEET_MODEL

    return ParakeetStreamingConfig(
        model=str(model_value).strip() or DEFAULT_PARAKEET_MODEL,
        device=str(pick("device", "cuda")).strip().lower() or "cuda",
        dtype=str(pick("dtype", "auto")).strip().lower() or "auto",
        max_gpu_memory_gb=max_gpu_memory if max_gpu_memory > 0 else None,
        cpu_fallback=pick_bool("cpu_fallback", True),
        eou_token=str(pick("eou_token", "<EOU>")).strip() or "<EOU>",
    )


def _is_memory_load_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "cuda out of memory",
            "outofmemoryerror",
            "paging file is too small",
            "os error 1455",
        )
    )


def _load_parakeet_model(config: ParakeetStreamingConfig) -> Any:
    key = (
        config.model,
        config.device,
        config.dtype,
        config.max_gpu_memory_gb,
        config.cpu_fallback,
        config.eou_token,
    )
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        import torch
        import nemo.collections.asr as nemo_asr
    except Exception as exc:  # pragma: no cover - exercised through caller error path
        raise RuntimeError(
            "Parakeet Realtime EOU STT dependencies are missing. "
            "Run: hermes tools post-setup parakeet_stt"
        ) from exc

    requested_device = config.device
    if requested_device == "auto":
        requested_device = "cuda" if torch.cuda.is_available() else "cpu"
    if requested_device == "cuda" and not torch.cuda.is_available():
        requested_device = "cpu"

    if requested_device == "cuda" and config.max_gpu_memory_gb:
        total_bytes = int(config.max_gpu_memory_gb * 1024**3)
        try:
            torch.cuda.set_per_process_memory_fraction(
                min(1.0, total_bytes / max(1, torch.cuda.get_device_properties(0).total_memory)),
                0,
            )
        except Exception:
            logger.debug("Could not apply Parakeet CUDA memory fraction", exc_info=True)

    logger.info("Loading Parakeet Realtime EOU STT model %s on %s", config.model, requested_device)
    try:
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=config.model)
        if hasattr(model, "to"):
            model = model.to(requested_device)
    except Exception as exc:
        if not config.cpu_fallback or requested_device == "cpu" or not _is_memory_load_error(exc):
            raise
        logger.warning("Parakeet CUDA load failed from memory pressure; retrying on CPU: %s", exc)
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=config.model)
        if hasattr(model, "to"):
            model = model.to("cpu")

    if hasattr(model, "eval"):
        model.eval()
    _MODEL_CACHE[key] = model
    return model


def warm_parakeet_stt(stt_config: dict[str, Any] | None = None) -> bool:
    _load_parakeet_model(resolve_parakeet_config(stt_config))
    return True


def parakeet_venv_python() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "parakeet-venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def parakeet_stdio_command() -> list[str]:
    return [str(parakeet_venv_python()), "-m", "tools.parakeet_streaming_stt", "--stdio"]


def _extract_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        if not result:
            return ""
        first = result[0]
        return str(getattr(first, "text", first) or "")
    return str(getattr(result, "text", result) or "")


def _strip_eou(text: str, token: str) -> str:
    return text.replace(token, " ").replace("<eou>", " ").strip()


class ParakeetStreamingSession:
    """Session fed by browser Float32 mic frames and finalized with Parakeet EOU."""

    # Re-transcribe the whole buffer every ~0.5s of new audio to produce a live
    # partial. ponytail: O(n^2) over the utterance (re-runs the growing buffer);
    # fine for short spoken turns, swap to NeMo cache-aware streaming inference
    # if long-form latency matters.
    _PARTIAL_INTERVAL_SAMPLES = 8000  # 0.5s at 16 kHz

    def __init__(
        self,
        stt_config: dict[str, Any] | None = None,
        *,
        loader: Callable[[ParakeetStreamingConfig], Any] = _load_parakeet_model,
        temp_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.config = resolve_parakeet_config(stt_config)
        self._loader = loader
        self._model: Any = None
        self._samples: list[float] = []
        self._since_last = 0
        self._last_partial = ""
        self.last_eou = False
        self._closed = False
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None

    def start(self) -> None:
        self._model = self._loader(self.config)

    def accept_samples(self, samples: Iterable[float]) -> None:
        if self._closed:
            return
        clamped = [float(max(-1.0, min(1.0, sample))) for sample in samples]
        self._samples.extend(clamped)
        self._since_last += len(clamped)

    def accept_bytes(self, chunk: bytes) -> str:
        """Accept a Float32 mic chunk; return a fresh partial transcript or ''.

        A partial is computed at most every ~0.5s of new audio so the helper
        emits live captions without re-transcribing on every tiny mic frame.
        """
        self.accept_samples(np.frombuffer(chunk, dtype=np.float32))
        if self._closed or self._since_last < self._PARTIAL_INTERVAL_SAMPLES or not self._samples:
            return ""
        if self._model is None:
            self.start()

        self._since_last = 0
        raw = self._transcribe_current()
        self.last_eou = self.config.eou_token.lower() in raw.lower()
        self._last_partial = _strip_eou(raw, self.config.eou_token)
        return self._last_partial

    def drain_text(self) -> str:
        return self._last_partial

    def finish(self) -> str:
        self._closed = True
        if not self._samples:
            return ""
        if self._model is None:
            self.start()
        return _strip_eou(self._transcribe_current(), self.config.eou_token)

    def _transcribe_current(self) -> str:
        temp_path = ""
        try:
            import soundfile as sf

            with tempfile.NamedTemporaryFile(
                prefix="hermes-parakeet-", suffix=".wav", dir=self._temp_dir, delete=False
            ) as tmp:
                temp_path = tmp.name
            sf.write(temp_path, np.asarray(self._samples, dtype=np.float32), 16000)
            return _extract_text(self._model.transcribe([temp_path], batch_size=1))
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def close(self) -> None:
        self._closed = True


def _emit_stdio(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _call_with_stdout_on_stderr(fn: Callable[[], Any]) -> Any:
    # NeMo logs to stdout on Windows; stdout is our JSON protocol.
    with redirect_stdout(sys.stderr):
        return fn()


def _run_stdio_server() -> int:
    session: ParakeetStreamingSession | None = None
    try:
        first_line = sys.stdin.readline()
        if not first_line:
            return 0
        first = json.loads(first_line)
        if first.get("type") != "start":
            _emit_stdio({"type": "error", "error": "Parakeet helper expected a start message"})
            return 2

        session = ParakeetStreamingSession(first.get("stt_config") if isinstance(first.get("stt_config"), dict) else {})
        _call_with_stdout_on_stderr(session.start)
        _emit_stdio({"type": "ready"})

        for line in sys.stdin:
            if not line:
                break
            payload = json.loads(line)
            event_type = payload.get("type")
            if event_type == "audio":
                raw = base64.b64decode(str(payload.get("data") or ""))
                partial = _call_with_stdout_on_stderr(lambda: session.accept_bytes(raw))
                if partial:
                    _emit_stdio({"type": "partial", "text": partial, "eou": session.last_eou})
                else:
                    _emit_stdio({"type": "ok", "eou": session.last_eou})
            elif event_type == "stop":
                final = _call_with_stdout_on_stderr(session.finish)
                _emit_stdio({"type": "final", "text": final})
                return 0
            else:
                _emit_stdio({"type": "error", "error": f"Unknown Parakeet helper event: {event_type}"})
                return 2
        return 0
    except BaseException as exc:  # noqa: BLE001
        logger.exception("Parakeet stdio helper failed")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        _emit_stdio({"type": "error", "error": str(exc)})
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parakeet Realtime EOU STT helper")
    parser.add_argument("--stdio", action="store_true", help="Run JSON-lines stdio streaming helper")
    args = parser.parse_args(argv)
    if args.stdio:
        return _run_stdio_server()
    parser.error("no mode selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
