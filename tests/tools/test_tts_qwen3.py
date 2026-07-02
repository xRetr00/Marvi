import json
import sys
import types
from pathlib import Path

from tools import tts_tool


class _FakeQwenModel:
    loaded = []
    clone_calls = []
    custom_calls = []
    design_calls = []
    clone_stream_calls = []

    @classmethod
    def from_pretrained(cls, model, **kwargs):
        cls.loaded.append((model, kwargs))
        return cls()

    def generate_voice_clone(self, **kwargs):
        self.clone_calls.append(kwargs)
        return [[0.0, 0.1, -0.1]], 24000

    def generate_voice_clone_streaming(self, **kwargs):
        self.clone_stream_calls.append(kwargs)
        yield [[0.0, 0.1, -0.1]], 24000, {"ttfa": 0.1}

    def generate_custom_voice(self, **kwargs):
        self.custom_calls.append(kwargs)
        return [[0.0, 0.1, -0.1]], 24000

    def generate_voice_design(self, **kwargs):
        self.design_calls.append(kwargs)
        return [[0.0, 0.1, -0.1]], 24000


def _install_fake_qwen(monkeypatch):
    _FakeQwenModel.loaded = []
    _FakeQwenModel.clone_calls = []
    _FakeQwenModel.custom_calls = []
    _FakeQwenModel.design_calls = []
    _FakeQwenModel.clone_stream_calls = []
    monkeypatch.setitem(
        sys.modules,
        "faster_qwen3_tts",
        types.SimpleNamespace(FasterQwen3TTS=_FakeQwenModel),
    )


def test_qwen3_is_builtin_provider():
    assert "qwen3" in tts_tool.BUILTIN_TTS_PROVIDERS
    assert tts_tool.PROVIDER_MAX_TEXT_LENGTH["qwen3"] > 0


def test_qwen3_clone_dispatches_and_writes_wav(tmp_path, monkeypatch):
    _install_fake_qwen(monkeypatch)
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "qwen3",
        "qwen3": {
            "model": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            "mode": "clone",
            "language": "English",
            "ref_audio": "ref.wav",
            "ref_text": "reference words",
            "chunk_size": 2,
        },
    })

    data = json.loads(tts_tool.text_to_speech_tool("hello", str(tmp_path / "out.wav")))

    assert data["success"] is True
    assert data["provider"] == "qwen3"
    assert Path(data["file_path"]).exists()
    assert _FakeQwenModel.loaded[0][0] == "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    assert _FakeQwenModel.loaded[0][1]["device"] == "cuda"
    assert _FakeQwenModel.clone_calls[0]["ref_audio"] == "ref.wav"
    assert _FakeQwenModel.clone_calls[0]["ref_text"] == "reference words"


def test_qwen3_custom_voice_and_warm_reuse_model(tmp_path, monkeypatch):
    _install_fake_qwen(monkeypatch)
    cfg = {
        "provider": "qwen3",
        "qwen3": {
            "model": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
            "mode": "custom",
            "speaker": "aiden",
        },
    }
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: cfg)

    assert tts_tool.warm_tts_provider(cfg) is True
    data = json.loads(tts_tool.text_to_speech_tool("hello", str(tmp_path / "out.wav")))

    assert data["success"] is True
    assert len(_FakeQwenModel.loaded) == 1
    assert _FakeQwenModel.custom_calls[0]["speaker"] == "aiden"


def test_qwen3_design_mode_passes_instruction(tmp_path, monkeypatch):
    _install_fake_qwen(monkeypatch)
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {
        "provider": "qwen3",
        "qwen3": {
            "model": "Qwen/Qwen3-TTS-12Hz-0.6B-VoiceDesign",
            "mode": "design",
            "instruct": "warm narrator",
        },
    })

    data = json.loads(tts_tool.text_to_speech_tool("hello", str(tmp_path / "out.wav")))

    assert data["success"] is True
    assert _FakeQwenModel.loaded[0][0] == "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
    assert _FakeQwenModel.design_calls[0]["instruct"] == "warm narrator"


def test_qwen3_streaming_chunks_start_audio_and_end(monkeypatch):
    _install_fake_qwen(monkeypatch)
    cfg = {
        "provider": "qwen3",
        "qwen3": {
            "model": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
            "mode": "clone",
            "ref_audio": "ref.wav",
            "ref_text": "reference words",
            "chunk_size": 1,
        },
    }
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: cfg)

    events = list(tts_tool.stream_text_to_speech_chunks("**hello**"))

    assert events[0] == {"type": "start", "sample_rate": 24000, "provider": "qwen3"}
    assert events[1]["type"] == "chunk"
    assert events[-1] == {"type": "end", "provider": "qwen3"}
    assert _FakeQwenModel.clone_stream_calls[0]["chunk_size"] == 1
