"""Streaming voice-activity gate for the wake word + STT paths.

Uses **TEN VAD** (https://github.com/TEN-framework/ten-vad) — an open-source
(Apache-2.0), 306 KB, frame-level VAD that outperforms Silero on precision,
speed and latency. It classifies *speech vs non-speech*, which rejects noisy-
but-non-speech audio (fans, TV, door slams) that a plain RMS energy gate lets
through — the research-backed fix for wake-word false positives and STT
hallucination on non-speech.

Graceful: if `ten-vad` isn't installed, ``SpeechGate.available`` is False and
callers fall back to their RMS energy gate.

    pip install ten-vad
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# TEN VAD processes fixed 16 ms hops at 16 kHz.
_HOP = 256


def _load_ten_vad(threshold: float):
    try:
        from ten_vad import TenVad
    except Exception:
        return None
    try:
        return TenVad(hop_size=_HOP, threshold=threshold)
    except TypeError:
        try:
            return TenVad()
        except Exception:
            return None
    except Exception:
        return None


class SpeechGate:
    """Feed streaming 16 kHz float samples; ask ``has_recent_speech()``.

    Tracks how many hops ago speech was last seen, so a caller can gate a
    detection on "was there speech within the last N ms".
    """

    def __init__(self, threshold: float = 0.5) -> None:
        self._vad = _load_ten_vad(threshold)
        self._buf = np.empty(0, dtype=np.int16)
        self._hops_since_speech = 1_000_000
        if self._vad is not None:
            logger.info("TEN VAD speech gate active (threshold=%.2f)", threshold)

    @property
    def available(self) -> bool:
        return self._vad is not None

    def accept(self, samples) -> None:
        if self._vad is None:
            return
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return
        pcm16 = np.clip(arr, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype(np.int16)
        self._buf = np.concatenate([self._buf, pcm16]) if self._buf.size else pcm16

        while self._buf.shape[0] >= _HOP:
            frame = self._buf[:_HOP]
            self._buf = self._buf[_HOP:]
            try:
                _prob, flag = self._vad.process(frame)
            except Exception:
                # A broken VAD must never wedge detection — disable + fall back.
                logger.exception("TEN VAD process failed; disabling")
                self._vad = None
                return
            self._hops_since_speech = 0 if flag else self._hops_since_speech + 1

    def has_recent_speech(self, within_ms: int = 1200) -> bool:
        within_hops = max(1, int(within_ms / 16))
        return self._hops_since_speech <= within_hops


def make_speech_gate(threshold: float = 0.5) -> Optional[SpeechGate]:
    """Return a SpeechGate, or None if TEN VAD is unavailable."""
    gate = SpeechGate(threshold)
    return gate if gate.available else None
