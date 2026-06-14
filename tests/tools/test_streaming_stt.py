"""Tests for local streaming STT helpers."""

import pytest


class _FakeStreamRecognizer:
    def __init__(self, partials=None, final="hello world"):
        self.started = False
        self.ended = False
        self.frames = []
        self.partials = list(partials or ["hello"])
        self.final_text = final

    def start(self, sample_rate=16000):
        self.started = True
        self.sample_rate = sample_rate

    def accept_waveform(self, samples):
        self.frames.append(list(samples))
        return self.partials.pop(0) if self.partials else ""

    def finish(self):
        self.ended = True
        return self.final_text


def test_streaming_config_defaults_to_disabled():
    from tools.streaming_stt import streaming_stt_config

    cfg = streaming_stt_config({"stt": {}})

    assert cfg.enabled is False
    assert cfg.provider == "sherpa_onnx"
    assert cfg.sample_rate == 16000


def test_streaming_config_reads_nested_settings():
    from tools.streaming_stt import streaming_stt_config

    cfg = streaming_stt_config(
        {
            "stt": {
                "streaming": {
                    "enabled": True,
                    "provider": "sherpa_onnx",
                    "model": "en-20m-int8",
                    "sample_rate": 8000,
                    "partial_interval_ms": 75,
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.model == "en-20m-int8"
    assert cfg.sample_rate == 8000
    assert cfg.partial_interval_ms == 75


def test_factory_returns_fake_recognizer_for_tests():
    from tools.streaming_stt import StreamingSttFactory

    recognizer = _FakeStreamRecognizer()
    factory = StreamingSttFactory(create_recognizer=lambda _cfg: recognizer)

    assert factory.create({"stt": {"streaming": {"enabled": True}}}) is recognizer


def test_factory_rejects_disabled_streaming():
    from tools.streaming_stt import StreamingSttFactory, StreamingSttUnavailable

    factory = StreamingSttFactory(create_recognizer=lambda _cfg: _FakeStreamRecognizer())

    with pytest.raises(StreamingSttUnavailable, match="disabled"):
        factory.create({"stt": {"streaming": {"enabled": False}}})
