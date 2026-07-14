"""Local streaming speech-to-text backed by moonshine-voice."""

from __future__ import annotations

import logging
import sys
import threading
from array import array
from typing import Any

from tools.lazy_deps import ensure


_MODEL_NAMES = {
    "tiny-streaming": "TINY_STREAMING",
    "base-streaming": "BASE_STREAMING",
    "small-streaming": "SMALL_STREAMING",
    "medium-streaming": "MEDIUM_STREAMING",
}

logger = logging.getLogger(__name__)


class MoonshineStreamingSession:
    """Small adapter matching the desktop streaming-STT session contract."""

    def __init__(self, stt_config: dict[str, Any] | None = None):
        ensure("stt.moonshine.windows" if sys.platform == "win32" else "stt.moonshine")
        from moonshine_voice import ModelArch, TranscriptEventListener, Transcriber, get_model_for_language

        streaming = (stt_config or {}).get("streaming", {})
        streaming = streaming if isinstance(streaming, dict) else {}
        nested = streaming.get("moonshine", {})
        nested = nested if isinstance(nested, dict) else {}
        language = str(nested.get("language") or "en").strip() or "en"
        requested = str(nested.get("model") or streaming.get("model") or "small-streaming").lower()
        requested = requested.removeprefix("moonshine-")
        device = str(nested.get("device") or "auto").strip().lower()
        if device not in {"auto", "cpu"}:
            raise ValueError(
                "Moonshine streaming STT supports device=auto or cpu; "
                "moonshine-voice does not expose CUDA execution."
            )
        arch = getattr(ModelArch, _MODEL_NAMES.get(requested, "SMALL_STREAMING"))
        model_path, model_arch = get_model_for_language(language, arch)

        self._lock = threading.Lock()
        self._partial = ""
        self._completed: list[str] = []
        self.last_eou = False
        self.last_eou_prob = 0.0
        self._started = False
        self._transcriber = Transcriber(model_path=model_path, model_arch=model_arch, update_interval=0.2)
        self.device = "cpu"
        logger.info("Moonshine streaming STT ready (model=%s language=%s device=cpu)", requested, language)
        owner = self

        class Listener(TranscriptEventListener):
            def on_line_text_changed(self, event):
                with owner._lock:
                    owner._partial = str(event.line.text or "").strip()

            def on_line_completed(self, event):
                text = str(event.line.text or "").strip()
                with owner._lock:
                    if text:
                        owner._completed.append(text)
                        owner._partial = text
                    owner.last_eou = True
                    owner.last_eou_prob = 1.0

        self._listener = Listener()
        self._transcriber.add_listener(self._listener)

    def begin(self) -> None:
        with self._lock:
            self._partial = ""
            self._completed = []
            self.last_eou = False
            self.last_eou_prob = 0.0
        self._transcriber.start()
        self._started = True

    def accept_bytes(self, chunk: bytes) -> str:
        samples = array("f")
        samples.frombytes(chunk[: len(chunk) - (len(chunk) % 4)])
        if sys.byteorder != "little":
            samples.byteswap()
        if samples:
            self._transcriber.add_audio(samples.tolist(), 16000)
        with self._lock:
            return self._partial

    def consume_eou(self) -> bool:
        """Consume one pause-delimited Moonshine line-completion event."""
        with self._lock:
            completed = self.last_eou
            self.last_eou = False
            self.last_eou_prob = 0.0
            return completed

    def finish(self) -> str:
        if self._started:
            self._transcriber.stop()
            self._started = False
        with self._lock:
            return " ".join(self._completed).strip() or self._partial

    def close(self) -> None:
        if self._started:
            try:
                self._transcriber.stop()
            except Exception:
                pass
            self._started = False
        self._transcriber.get_default_stream().close()
        self._transcriber.close()
