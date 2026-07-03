"""Local Nemotron RNNT streaming speech-to-text for desktop mic audio."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_NEMOTRON_MODEL = "nvidia/nemotron-speech-streaming-en-0.6b"


@dataclass(frozen=True)
class NemotronStreamingConfig:
    model: str = DEFAULT_NEMOTRON_MODEL
    lookahead_tokens: int = 1
    device_map: str = "auto"
    dtype: str = "auto"


_MODEL_CACHE: dict[tuple[str, int, str, str], tuple[Any, Any, Any]] = {}
_MODEL_LOCK = threading.Lock()


def resolve_nemotron_config(stt_config: dict[str, Any] | None) -> NemotronStreamingConfig:
    streaming = (stt_config or {}).get("streaming", {})
    streaming = streaming if isinstance(streaming, dict) else {}
    nested = streaming.get("nemotron", {})
    nested = nested if isinstance(nested, dict) else {}

    def pick(key: str, default: Any) -> Any:
        value = nested.get(key, streaming.get(key, default))
        return default if value in (None, "") else value

    try:
        lookahead = max(0, int(pick("lookahead_tokens", 1)))
    except (TypeError, ValueError):
        lookahead = 1

    nested_model = nested.get("model")
    legacy_model = streaming.get("model")
    model_value = nested_model
    if model_value in (None, ""):
        legacy_model_text = str(legacy_model or "").strip()
        model_value = legacy_model_text if "nemotron" in legacy_model_text.lower() else DEFAULT_NEMOTRON_MODEL

    return NemotronStreamingConfig(
        model=str(model_value).strip() or DEFAULT_NEMOTRON_MODEL,
        lookahead_tokens=lookahead,
        device_map=str(pick("device_map", "auto")).strip() or "auto",
        dtype=str(pick("dtype", "auto")).strip().lower() or "auto",
    )


def _load_nemotron_model(config: NemotronStreamingConfig) -> tuple[Any, Any, Any]:
    key = (config.model, config.lookahead_tokens, config.device_map, config.dtype)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached

        try:
            import torch
            from transformers import AutoModelForRNNT, AutoProcessor, TextIteratorStreamer
        except Exception as exc:  # pragma: no cover - exercised through caller error path
            raise RuntimeError(
                "Nemotron streaming STT dependencies are missing. "
                "Run: hermes tools post-setup nemotron_stt"
            ) from exc

        processor = AutoProcessor.from_pretrained(config.model)
        if hasattr(processor, "set_num_lookahead_tokens"):
            processor.set_num_lookahead_tokens(config.lookahead_tokens)

        kwargs: dict[str, Any] = {}
        if config.device_map:
            kwargs["device_map"] = config.device_map
        if config.dtype in {"float16", "bfloat16", "float32"}:
            kwargs["torch_dtype"] = getattr(torch, config.dtype)

        logger.info(
            "Loading Nemotron streaming STT model %s (lookahead=%s, device_map=%s, dtype=%s)",
            config.model,
            config.lookahead_tokens,
            config.device_map,
            config.dtype,
        )
        model = AutoModelForRNNT.from_pretrained(config.model, **kwargs)
        if hasattr(model, "eval"):
            model.eval()
        cached = (processor, model, TextIteratorStreamer)
        _MODEL_CACHE[key] = cached
        return cached


def warm_nemotron_stt(stt_config: dict[str, Any] | None = None) -> bool:
    _load_nemotron_model(resolve_nemotron_config(stt_config))
    return True


def whisperlive_venv_python() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "whisperlive-venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def nemotron_stdio_command() -> list[str]:
    return [str(whisperlive_venv_python()), "-m", "tools.nemotron_streaming_stt", "--stdio"]


def _model_device(model: Any) -> Any:
    device = getattr(model, "device", None)
    if device is not None:
        return device
    try:
        return next(model.parameters()).device
    except Exception:
        return None


def _move_inputs(inputs: Any, model: Any) -> Any:
    if not hasattr(inputs, "to"):
        return inputs
    kwargs: dict[str, Any] = {}
    device = _model_device(model)
    if device is not None:
        kwargs["device"] = device
    dtype = getattr(model, "dtype", None)
    if dtype is not None:
        kwargs["dtype"] = dtype
    return inputs.to(**kwargs)


class NemotronStreamingSession:
    """Threaded RNNT generation session fed by browser Float32 mic frames."""

    def __init__(
        self,
        stt_config: dict[str, Any] | None = None,
        *,
        loader: Callable[[NemotronStreamingConfig], tuple[Any, Any, Any]] = _load_nemotron_model,
    ) -> None:
        self.config = resolve_nemotron_config(stt_config)
        self._loader = loader
        self._processor: Any = None
        self._model: Any = None
        self._streamer_cls: Any = None
        self._streamer: Any = None
        self._samples: Deque[float] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._started = False
        self._first_inputs: Any = None
        self._text_parts: list[str] = []
        self._drained_parts = 0
        self._error: BaseException | None = None
        self._generate_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None

    def start(self) -> None:
        self._processor, self._model, self._streamer_cls = self._loader(self.config)

    def accept_samples(self, samples: Iterable[float]) -> None:
        with self._condition:
            self._samples.extend(float(max(-1.0, min(1.0, sample))) for sample in samples)
            self._condition.notify_all()
            if not self._started and len(self._samples) >= self._first_chunk_samples():
                self._start_generation_locked()

    def finish(self) -> str:
        with self._condition:
            self._closed = True
            if not self._started and self._samples:
                self._start_generation_locked(pad_first=True)
            self._condition.notify_all()

        for thread in (self._generate_thread, self._reader_thread):
            if thread is not None:
                thread.join(timeout=120)

        if self._error is not None:
            raise RuntimeError(f"Nemotron streaming STT failed: {self._error}") from self._error
        return "".join(self._text_parts).strip()

    def drain_text(self) -> str:
        parts = self._text_parts[self._drained_parts :]
        self._drained_parts = len(self._text_parts)
        return "".join(parts).strip()

    def _first_chunk_samples(self) -> int:
        return int(getattr(self._processor, "num_samples_first_audio_chunk", 16000) or 16000)

    def _next_chunk_samples(self) -> int:
        return int(getattr(self._processor, "num_samples_per_audio_chunk", 2560) or 2560)

    def _start_generation_locked(self, *, pad_first: bool = False) -> None:
        if self._started:
            return
        first = self._pop_samples_locked(self._first_chunk_samples(), pad=pad_first)
        if not first:
            return
        self._first_inputs = self._features(first, first=True)
        self._streamer = self._streamer_cls(
            self._processor.tokenizer,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )
        self._started = True
        self._generate_thread = threading.Thread(target=self._run_generate, daemon=True, name="nemotron-stt-generate")
        self._reader_thread = threading.Thread(target=self._read_streamer, daemon=True, name="nemotron-stt-reader")
        self._generate_thread.start()
        self._reader_thread.start()

    def _pop_samples_locked(self, count: int, *, pad: bool = False) -> list[float]:
        out: list[float] = []
        while self._samples and len(out) < count:
            out.append(self._samples.popleft())
        if pad and out and len(out) < count:
            out.extend([0.0] * (count - len(out)))
        return out

    def _take_next_samples(self) -> Optional[list[float]]:
        count = self._next_chunk_samples()
        with self._condition:
            while not self._closed and len(self._samples) < count:
                self._condition.wait(timeout=0.25)
            if not self._samples:
                return None
            return self._pop_samples_locked(count, pad=self._closed)

    def _features(self, samples: list[float], *, first: bool = False) -> Any:
        inputs = self._processor(
            np.asarray(samples, dtype=np.float32),
            sampling_rate=16000,
            is_streaming=True,
            is_first_audio_chunk=first,
            return_tensors="pt",
        )
        return _move_inputs(inputs, self._model)

    def _feature_generator(self) -> Iterable[Any]:
        first = self._first_inputs
        if first is not None:
            features = first.input_features
            first_mel_frames = getattr(self._processor, "num_mel_frames_first_audio_chunk", None)
            if first_mel_frames:
                features = features[:, : int(first_mel_frames), :]
            yield features
        while True:
            samples = self._take_next_samples()
            if samples is None:
                return
            yield self._features(samples, first=False).input_features

    def _run_generate(self) -> None:
        try:
            first_inputs = dict(self._first_inputs or {})
            first_inputs["input_features"] = self._feature_generator()
            first_inputs["streamer"] = self._streamer
            self._model.generate(
                **first_inputs,
            )
        except TypeError:
            # Older examples used input_features as the generator. Keep this
            # fallback so model-card API drift does not break the desktop path.
            try:
                self._model.generate(
                    input_features=self._feature_generator(),
                    streamer=self._streamer,
                )
            except BaseException as exc:  # noqa: BLE001
                self._error = exc
                self._close_streamer()
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._close_streamer()

    def _read_streamer(self) -> None:
        try:
            for text in self._streamer:
                if text:
                    self._text_parts.append(str(text))
        except BaseException as exc:  # noqa: BLE001
            self._error = exc

    def _close_streamer(self) -> None:
        streamer = self._streamer
        if streamer is None:
            return
        end = getattr(streamer, "end", None)
        if callable(end):
            end()
            return
        items = getattr(streamer, "items", None)
        put = getattr(items, "put", None)
        if callable(put):
            put(None)


def _emit_stdio(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _run_stdio_server() -> int:
    session: NemotronStreamingSession | None = None
    try:
        first_line = sys.stdin.readline()
        if not first_line:
            return 0
        first = json.loads(first_line)
        if first.get("type") != "start":
            _emit_stdio({"type": "error", "error": "Nemotron helper expected a start message"})
            return 2

        session = NemotronStreamingSession(first.get("stt_config") if isinstance(first.get("stt_config"), dict) else {})
        session.start()
        _emit_stdio({"type": "ready"})

        for line in sys.stdin:
            if not line:
                break
            payload = json.loads(line)
            event_type = payload.get("type")
            if event_type == "audio":
                raw = base64.b64decode(str(payload.get("data") or ""))
                samples = np.frombuffer(raw, dtype=np.float32)
                session.accept_samples(samples)
                partial = session.drain_text()
                _emit_stdio({"type": "partial", "text": partial} if partial else {"type": "ok"})
            elif event_type == "stop":
                final = session.finish()
                _emit_stdio({"type": "final", "text": final})
                return 0
            else:
                _emit_stdio({"type": "error", "error": f"Unknown Nemotron helper event: {event_type}"})
                return 2
        return 0
    except BaseException as exc:  # noqa: BLE001
        logger.exception("Nemotron stdio helper failed")
        _emit_stdio({"type": "error", "error": str(exc)})
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nemotron streaming STT helper")
    parser.add_argument("--stdio", action="store_true", help="Run JSON-lines stdio streaming helper")
    args = parser.parse_args(argv)
    if args.stdio:
        return _run_stdio_server()
    parser.error("no mode selected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
