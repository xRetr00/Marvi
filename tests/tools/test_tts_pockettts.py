"""Tests for the PocketTTS local provider in tools/tts_tool.py."""

import json
import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)


@pytest.fixture(autouse=True)
def clear_pockettts_cache():
    from tools import tts_tool

    getattr(tts_tool, "_pockettts_model_cache", {}).clear()
    getattr(tts_tool, "_pockettts_voice_cache", {}).clear()
    yield
    getattr(tts_tool, "_pockettts_model_cache", {}).clear()
    getattr(tts_tool, "_pockettts_voice_cache", {}).clear()


class _FakeAudio:
    def numpy(self):
        return [0, 0, 0, 0]


class _FakeTTSModel:
    sample_rate = 24000

    def __init__(self):
        self.get_state_for_audio_prompt = MagicMock(return_value="voice-state")
        self.generate_audio = MagicMock(return_value=_FakeAudio())


@pytest.fixture
def mock_pockettts_modules(monkeypatch):
    fake_model = _FakeTTSModel()
    fake_cls = MagicMock()
    fake_cls.load_model.return_value = fake_model

    fake_pocket_tts = types.ModuleType("pocket_tts")
    fake_pocket_tts.TTSModel = fake_cls

    fake_wavfile = types.ModuleType("scipy.io.wavfile")
    fake_wavfile.write = MagicMock(side_effect=lambda path, rate, audio: open(path, "wb").write(b"RIFFWAVE"))

    fake_io = types.ModuleType("scipy.io")
    fake_io.wavfile = fake_wavfile

    fake_scipy = types.ModuleType("scipy")
    fake_scipy.io = fake_io

    monkeypatch.setitem(sys.modules, "pocket_tts", fake_pocket_tts)
    monkeypatch.setitem(sys.modules, "scipy", fake_scipy)
    monkeypatch.setitem(sys.modules, "scipy.io", fake_io)
    monkeypatch.setitem(sys.modules, "scipy.io.wavfile", fake_wavfile)

    return fake_model, fake_cls, fake_wavfile


class TestGeneratePocketTts:
    def test_successful_wav_generation(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, fake_cls, fake_wavfile = mock_pockettts_modules
        output_path = str(tmp_path / "test.wav")

        result = _generate_pockettts("Hello world", output_path, {})

        assert result == output_path
        assert (tmp_path / "test.wav").exists()
        fake_cls.load_model.assert_called_once()
        fake_model.get_state_for_audio_prompt.assert_called_once_with("alba")
        fake_model.generate_audio.assert_called_once_with("voice-state", "Hello world")
        fake_wavfile.write.assert_called_once()

    def test_config_passes_voice(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, _, _ = mock_pockettts_modules

        _generate_pockettts(
            "Hi",
            str(tmp_path / "out.wav"),
            {"pockettts": {"voice": "marius"}},
        )

        fake_model.get_state_for_audio_prompt.assert_called_once_with("marius")

    def test_known_preset_voice_is_case_normalized(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, _, _ = mock_pockettts_modules

        _generate_pockettts(
            "Hi",
            str(tmp_path / "out.wav"),
            {"pockettts": {"voice": "JANE"}},
        )

        fake_model.get_state_for_audio_prompt.assert_called_once_with("jane")

    def test_model_and_voice_are_cached(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, fake_cls, _ = mock_pockettts_modules

        _generate_pockettts("One", str(tmp_path / "a.wav"), {})
        _generate_pockettts("Two", str(tmp_path / "b.wav"), {})

        fake_cls.load_model.assert_called_once()
        fake_model.get_state_for_audio_prompt.assert_called_once()
        assert fake_model.generate_audio.call_count == 2

    def test_different_configured_voices_use_distinct_cached_voice_states(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, fake_cls, _ = mock_pockettts_modules
        fake_model.get_state_for_audio_prompt.side_effect = lambda voice: f"state:{voice}"

        _generate_pockettts(
            "One",
            str(tmp_path / "a.wav"),
            {"pockettts": {"voice": "alba"}},
        )
        _generate_pockettts(
            "Two",
            str(tmp_path / "b.wav"),
            {"pockettts": {"voice": "marius"}},
        )

        fake_cls.load_model.assert_called_once()
        assert fake_model.get_state_for_audio_prompt.call_args_list[0].args == ("alba",)
        assert fake_model.get_state_for_audio_prompt.call_args_list[1].args == ("marius",)
        assert fake_model.generate_audio.call_args_list[0].args[:2] == ("state:alba", "One")
        assert fake_model.generate_audio.call_args_list[1].args[:2] == ("state:marius", "Two")

    def test_warm_pockettts_preloads_model_and_selected_voice(self, mock_pockettts_modules):
        from tools.tts_tool import warm_tts_provider

        fake_model, fake_cls, _ = mock_pockettts_modules

        warmed = warm_tts_provider({"provider": "pockettts", "pockettts": {"voice": "cosette"}})

        assert warmed is True
        fake_cls.load_model.assert_called_once()
        fake_model.get_state_for_audio_prompt.assert_called_once_with("cosette")
        fake_model.generate_audio.assert_not_called()


class TestCheckPocketTtsAvailable:
    def test_reports_available_when_package_present(self, monkeypatch):
        import importlib.util
        from tools.tts_tool import _check_pockettts_available

        fake_spec = MagicMock()
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name: fake_spec if name == "pocket_tts" else None,
        )

        assert _check_pockettts_available() is True

    def test_reports_unavailable_when_package_missing(self, monkeypatch):
        import importlib.util
        from tools.tts_tool import _check_pockettts_available

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

        assert _check_pockettts_available() is False


class TestDispatcherBranch:
    def test_dispatches_to_pockettts(self, tmp_path, monkeypatch, mock_pockettts_modules):
        from tools import tts_tool

        monkeypatch.setattr(
            tts_tool,
            "_load_tts_config",
            lambda: {"provider": "pockettts", "pockettts": {"voice": "marius"}},
        )

        result = json.loads(
            tts_tool.text_to_speech_tool("hello", output_path=str(tmp_path / "clip.wav"))
        )

        assert result["success"] is True
        assert result["provider"] == "pockettts"

    def test_pockettts_not_installed_returns_helpful_error(self, tmp_path, monkeypatch):
        from tools import tts_tool

        def raise_import():
            raise ImportError("No module named pocket_tts")

        monkeypatch.setattr(tts_tool, "_import_pockettts_model", raise_import)
        monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "pockettts"})

        result = json.loads(
            tts_tool.text_to_speech_tool("hello", output_path=str(tmp_path / "clip.wav"))
        )

        assert result["success"] is False
        assert "pocket-tts" in result["error"].lower()
