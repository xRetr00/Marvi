from __future__ import annotations

import struct
import sys
import types

import pytest

from tools import moonshine_streaming_stt


class FakeModelArch:
    TINY_STREAMING = 2
    BASE_STREAMING = 3
    SMALL_STREAMING = 4
    MEDIUM_STREAMING = 5


class FakeListener:
    pass


class FakeTranscriber:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.listener = None
        self.audio = []
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def add_listener(self, listener):
        self.listener = listener

    def start(self):
        self.started = True

    def add_audio(self, samples, sample_rate):
        self.audio.append((samples, sample_rate))
        line = types.SimpleNamespace(text="hello")
        self.listener.on_line_text_changed(types.SimpleNamespace(line=line))

    def stop(self):
        line = types.SimpleNamespace(text="hello world")
        self.listener.on_line_completed(types.SimpleNamespace(line=line))

    def close(self):
        self.closed = True

    def get_default_stream(self):
        return self


def test_moonshine_session_streams_float32_and_reports_eou(monkeypatch):
    monkeypatch.setattr(moonshine_streaming_stt, "ensure", lambda _feature: None)
    fake_module = types.SimpleNamespace(
        ModelArch=FakeModelArch,
        TranscriptEventListener=FakeListener,
        Transcriber=FakeTranscriber,
        get_model_for_language=lambda language, arch: (f"/{language}/model", arch),
    )
    monkeypatch.setitem(sys.modules, "moonshine_voice", fake_module)
    FakeTranscriber.instances.clear()

    session = moonshine_streaming_stt.MoonshineStreamingSession(
        {"streaming": {"moonshine": {"language": "en", "model": "tiny-streaming"}}}
    )
    assert session.device == "cpu"
    session.begin()
    partial = session.accept_bytes(struct.pack("<ff", 0.25, -0.5))

    assert partial == "hello"
    assert FakeTranscriber.instances[0].kwargs["model_arch"] == FakeModelArch.TINY_STREAMING
    assert FakeTranscriber.instances[0].audio == [([0.25, -0.5], 16000)]
    assert session.finish() == "hello world"
    assert session.last_eou is True


def test_moonshine_rejects_fake_cuda_device(monkeypatch):
    monkeypatch.setattr(moonshine_streaming_stt, "ensure", lambda _feature: None)
    fake_module = types.SimpleNamespace(
        ModelArch=FakeModelArch,
        TranscriptEventListener=FakeListener,
        Transcriber=FakeTranscriber,
        get_model_for_language=lambda language, arch: (f"/{language}/model", arch),
    )
    monkeypatch.setitem(sys.modules, "moonshine_voice", fake_module)

    with pytest.raises(ValueError, match="does not expose CUDA"):
        moonshine_streaming_stt.MoonshineStreamingSession(
            {"streaming": {"moonshine": {"device": "cuda"}}}
        )


def test_moonshine_line_completion_is_consumed_once(monkeypatch):
    monkeypatch.setattr(moonshine_streaming_stt, "ensure", lambda _feature: None)
    fake_module = types.SimpleNamespace(
        ModelArch=FakeModelArch,
        TranscriptEventListener=FakeListener,
        Transcriber=FakeTranscriber,
        get_model_for_language=lambda language, arch: (f"/{language}/model", arch),
    )
    monkeypatch.setitem(sys.modules, "moonshine_voice", fake_module)

    session = moonshine_streaming_stt.MoonshineStreamingSession()
    session.begin()
    session._listener.on_line_completed(types.SimpleNamespace(line=types.SimpleNamespace(text="done")))

    assert session.consume_eou() is True
    assert session.consume_eou() is False
