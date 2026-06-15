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


def test_wake_word_config_defaults_include_marvi_variants():
    from tools.streaming_stt import wake_word_config

    cfg = wake_word_config({"voice": {}})

    assert cfg.enabled is False
    assert cfg.provider == "sherpa_onnx"
    assert "hey marvi" in cfg.phrases
    assert "marvi" in cfg.phrases
    assert "marve" in cfg.phrases
    assert "marfe" in cfg.phrases
    assert "marfi" in cfg.phrases


def test_wake_word_config_reads_nested_settings():
    from tools.streaming_stt import wake_word_config

    cfg = wake_word_config(
        {
            "voice": {
                "wake_word": {
                    "enabled": True,
                    "phrases": ["Hey Marvi", "marfe", "", "hey marvi"],
                    "threshold": 0.42,
                    "boost": 2.5,
                    "command_timeout_ms": 9000,
                    "cooldown_ms": 500,
                }
            }
        }
    )

    assert cfg.enabled is True
    assert cfg.phrases == ("hey marvi", "marfe")
    assert cfg.threshold == 0.42
    assert cfg.boost == 2.5
    assert cfg.command_timeout_ms == 9000
    assert cfg.cooldown_ms == 500


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


def test_missing_sherpa_error_points_to_setup(monkeypatch):
    from tools import streaming_stt
    from tools.streaming_stt import StreamingSttUnavailable

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(StreamingSttUnavailable, match="hermes tools post-setup sherpa_onnx"):
        streaming_stt._import_sherpa_onnx()


def test_wake_word_factory_returns_fake_spotter_for_tests():
    from tools.streaming_stt import WakeWordFactory

    class FakeSpotter:
        pass

    spotter = FakeSpotter()
    factory = WakeWordFactory(create_spotter=lambda _cfg: spotter)

    assert factory.create({"voice": {"wake_word": {"enabled": True}}}) is spotter


def test_wake_word_factory_rejects_disabled_config():
    from tools.streaming_stt import StreamingSttUnavailable, WakeWordFactory

    factory = WakeWordFactory(create_spotter=lambda _cfg: object())

    with pytest.raises(StreamingSttUnavailable, match="disabled"):
        factory.create({"voice": {"wake_word": {"enabled": False}}})


def test_prepare_wake_word_assets_resolves_model_and_keywords(monkeypatch):
    from tools import streaming_stt

    calls = []

    monkeypatch.setattr(
        streaming_stt,
        "resolve_sherpa_kws_model_files",
        lambda cfg: calls.append(("resolve", cfg.enabled, cfg.provider, cfg.phrases)) or {"tokens": "tokens", "bpe_model": "bpe"},
    )
    monkeypatch.setattr(
        streaming_stt,
        "_write_wake_keywords_file",
        lambda cfg, files: calls.append(("keywords", cfg.enabled, files["tokens"])) or "keywords.txt",
    )

    keywords = streaming_stt.prepare_wake_word_assets(
        {"voice": {"wake_word": {"enabled": False, "phrases": ["Hey Marvi"], "threshold": 0.33}}}
    )

    assert keywords == "keywords.txt"
    assert calls == [
        ("resolve", True, "sherpa_onnx", ("hey marvi",)),
        ("keywords", True, "tokens"),
    ]


def test_wake_keywords_tokenizer_forces_utf8_stdio(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from tools import streaming_stt
    from tools.streaming_stt import WakeWordConfig

    calls = []
    monkeypatch.setattr(streaming_stt.shutil, "which", lambda _name: "sherpa-onnx-cli")
    monkeypatch.setattr(streaming_stt, "_model_cache_dir", lambda _model: tmp_path)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(streaming_stt.subprocess, "run", fake_run)

    streaming_stt._write_wake_keywords_file(
        WakeWordConfig(enabled=True, phrases=("hey marvi",)),
        {"tokens": "tokens.txt", "bpe_model": "bpe.model"},
    )

    assert calls
    assert calls[0][1]["env"]["PYTHONIOENCODING"] == "utf-8"
