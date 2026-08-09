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
        fake_cls.load_model.assert_called_once_with(
            language="english", temp=0.7, lsd_decode_steps=1, quantize=False
        )
        fake_model.get_state_for_audio_prompt.assert_called_once_with("alba")
        fake_model.generate_audio.assert_called_once_with("voice-state", "Hello world")
        fake_wavfile.write.assert_called_once()

    def test_cuda_audio_moves_to_cpu_before_numpy(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, _, _ = mock_pockettts_modules
        cuda_audio = MagicMock()
        cpu_audio = _FakeAudio()
        cuda_audio.detach.return_value = cuda_audio
        cuda_audio.cpu.return_value = cpu_audio
        fake_model.generate_audio.return_value = cuda_audio

        _generate_pockettts("Hello", str(tmp_path / "cuda.wav"), {})

        cuda_audio.detach.assert_called_once()
        cuda_audio.cpu.assert_called_once()

    def test_config_passes_voice(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        fake_model, _, _ = mock_pockettts_modules

        _generate_pockettts(
            "Hi",
            str(tmp_path / "out.wav"),
            {"pockettts": {"voice": "marius"}},
        )

        fake_model.get_state_for_audio_prompt.assert_called_once_with("marius")

    def test_pockettts_21_model_options_are_forwarded_and_cached_separately(
        self, tmp_path, mock_pockettts_modules
    ):
        from tools.tts_tool import _generate_pockettts

        _fake_model, fake_cls, _ = mock_pockettts_modules
        config = {
            "pockettts": {
                "language": "english_2026-04",
                "temperature": 0.55,
                "lsd_decode_steps": 2,
                "quantize": True,
            }
        }

        _generate_pockettts("Hi", str(tmp_path / "one.wav"), config)
        _generate_pockettts("Again", str(tmp_path / "two.wav"), config)

        fake_cls.load_model.assert_called_once_with(
            language="english_2026-04",
            temp=0.55,
            lsd_decode_steps=2,
            quantize=True,
        )

    def test_custom_model_config_replaces_language(self, tmp_path, mock_pockettts_modules):
        from tools.tts_tool import _generate_pockettts

        _fake_model, fake_cls, _ = mock_pockettts_modules
        _generate_pockettts(
            "Hi",
            str(tmp_path / "custom.wav"),
            {"pockettts": {"config": "D:/voices/pocket.yaml"}},
        )

        fake_cls.load_model.assert_called_once_with(
            config="D:/voices/pocket.yaml",
            temp=0.7,
            lsd_decode_steps=1,
            quantize=False,
        )

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

    def test_concurrent_warm_uses_single_model_load(self, mock_pockettts_modules):
        import concurrent.futures
        import time

        from tools.tts_tool import warm_tts_provider

        fake_model, fake_cls, _ = mock_pockettts_modules

        def slow_load_model(**_kwargs):
            time.sleep(0.05)
            return fake_model

        fake_cls.load_model.side_effect = slow_load_model

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: warm_tts_provider({"provider": "pockettts", "pockettts": {"voice": "cosette"}}),
                    range(8),
                )
            )

        assert results == [True] * 8
        fake_cls.load_model.assert_called_once()
        fake_model.get_state_for_audio_prompt.assert_called_once_with("cosette")

    def test_concurrent_generations_never_share_mutable_model_state(
        self, tmp_path, mock_pockettts_modules
    ):
        import concurrent.futures
        import threading
        import time

        from tools.tts_tool import _generate_pockettts

        fake_model, _, _ = mock_pockettts_modules
        guard = threading.Lock()
        active = 0
        max_active = 0

        def generate(_voice, _text):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return _FakeAudio()

        fake_model.generate_audio.side_effect = generate
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    _generate_pockettts,
                    f"Sentence {index}",
                    str(tmp_path / f"{index}.wav"),
                    {},
                )
                for index in range(2)
            ]
            for future in futures:
                future.result()

        assert max_active == 1

    def test_unload_tts_provider_drops_cache_and_reloads_on_next_use(self, mock_pockettts_modules):
        from tools import tts_tool

        fake_model, fake_cls, _ = mock_pockettts_modules

        tts_tool.warm_tts_provider({"provider": "pockettts", "pockettts": {"voice": "alba"}})
        fake_cls.load_model.assert_called_once()
        assert tts_tool._pockettts_model_cache
        assert tts_tool._pockettts_voice_cache

        tts_tool.unload_tts_provider()

        assert not tts_tool._pockettts_model_cache
        assert not tts_tool._pockettts_voice_cache

        # Next warm re-loads lazily (promotion is free).
        tts_tool.warm_tts_provider({"provider": "pockettts", "pockettts": {"voice": "alba"}})
        assert fake_cls.load_model.call_count == 2

    def test_streaming_chunks_start_audio_and_end(self, monkeypatch, mock_pockettts_modules):
        from tools import tts_tool

        monkeypatch.setattr(
            tts_tool,
            "_load_tts_config",
            lambda: {"provider": "pockettts", "pockettts": {"voice": "marius"}},
        )

        events = list(tts_tool.stream_text_to_speech_chunks("**hello**"))

        assert events[0] == {"type": "start", "sample_rate": 24000, "provider": "pockettts"}
        assert any(event.get("type") == "chunk" and event.get("audio") for event in events)
        assert events[-1] == {"type": "end", "provider": "pockettts"}

    def test_streaming_uses_pockettts_native_generator(self, monkeypatch, mock_pockettts_modules):
        from tools import tts_tool

        fake_model, _, _ = mock_pockettts_modules
        fake_model.generate_audio_stream = MagicMock(
            return_value=iter((_FakeAudio(), _FakeAudio()))
        )
        monkeypatch.setattr(
            tts_tool,
            "_load_tts_config",
            lambda: {"provider": "pockettts", "pockettts": {"voice": "jane"}},
        )

        events = list(tts_tool.stream_text_to_speech_chunks("hello"))

        fake_model.generate_audio_stream.assert_called_once_with("voice-state", "hello")
        fake_model.generate_audio.assert_not_called()
        assert sum(event.get("type") == "chunk" for event in events) == 2


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
        from tools import lazy_deps
        from tools.tts_tool import _check_pockettts_available

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("offline")

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(lazy_deps, "ensure", unavailable)

        assert _check_pockettts_available() is False

    def test_repairs_pruned_package_with_lazy_install(self, monkeypatch):
        import importlib.util
        from tools import lazy_deps
        from tools.tts_tool import _check_pockettts_available

        installed = False

        def find_spec(name):
            return MagicMock() if name == "pocket_tts" and installed else None

        def ensure(feature, *, prompt):
            nonlocal installed
            assert (feature, prompt) == ("tts.pockettts", False)
            installed = True

        monkeypatch.setattr(importlib.util, "find_spec", find_spec)
        monkeypatch.setattr(lazy_deps, "ensure", ensure)

        assert _check_pockettts_available() is True


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


class TestSplitForStreaming:
    """Clause splitter that drives low-latency streaming TTS (duplex phase 1)."""

    def test_flushes_each_sentence_end_for_early_audio(self):
        from tools.tts_tool import _split_for_streaming

        segments = _split_for_streaming("Hi. How are you doing today? I am fine.")

        # Every sentence end flushes (even a short "Hi.") so audio starts ASAP.
        assert segments == ["Hi.", "How are you doing today?", "I am fine."]

    def test_short_comma_fragments_merge_until_min_length(self):
        from tools.tts_tool import _split_for_streaming

        # Sub-clause fragments with no sentence end merge until they're long
        # enough, so we don't synthesize choppy one-word snippets.
        assert _split_for_streaming("a, b, c, this is now long enough to flush") == [
            "a, b, c, this is now long enough to flush"
        ]

    def test_long_clause_flushes_early_for_first_audio(self):
        from tools.tts_tool import _split_for_streaming

        segments = _split_for_streaming(
            "Well, that is a genuinely long opening clause, and then more."
        )

        # The first comma-clause is past the min length, so it streams first
        # instead of waiting for the whole sentence.
        assert len(segments) >= 2
        assert segments[0] == "Well, that is a genuinely long opening clause,"

    def test_no_delimiters_returns_single_segment(self):
        from tools.tts_tool import _split_for_streaming

        assert _split_for_streaming("just a few words") == ["just a few words"]
