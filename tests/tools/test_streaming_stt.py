"""Tests for local wake-word helpers."""

import pytest


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


def test_missing_sherpa_error_points_to_setup(monkeypatch):
    from tools import streaming_stt
    from tools.streaming_stt import WakeWordUnavailable

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "sherpa_onnx":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(WakeWordUnavailable, match="hermes tools post-setup sherpa_onnx"):
        streaming_stt._import_sherpa_onnx()


def test_wake_word_factory_returns_fake_spotter_for_tests():
    from tools.streaming_stt import WakeWordFactory

    class FakeSpotter:
        pass

    spotter = FakeSpotter()
    factory = WakeWordFactory(create_spotter=lambda _cfg: spotter)

    assert factory.create({"voice": {"wake_word": {"enabled": True}}}) is spotter


def test_wake_word_factory_rejects_disabled_config():
    from tools.streaming_stt import WakeWordUnavailable, WakeWordFactory

    factory = WakeWordFactory(create_spotter=lambda _cfg: object())

    with pytest.raises(WakeWordUnavailable, match="disabled"):
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


def test_kws_model_resolution_prefers_full_precision_files(tmp_path):
    from tools.streaming_stt import WakeWordConfig, resolve_sherpa_kws_model_files

    root = tmp_path / "kws"
    root.mkdir()
    for name in [
        "encoder-epoch-1.int8.onnx",
        "encoder-epoch-1.onnx",
        "decoder-epoch-1.int8.onnx",
        "decoder-epoch-1.onnx",
        "joiner-epoch-1.int8.onnx",
        "joiner-epoch-1.onnx",
        "tokens.txt",
        "bpe.model",
    ]:
        (root / name).write_text("x", encoding="utf-8")

    files = resolve_sherpa_kws_model_files(WakeWordConfig(enabled=True, model=str(root)))

    assert files["encoder"].endswith("encoder-epoch-1.onnx")
    assert files["decoder"].endswith("decoder-epoch-1.onnx")
    assert files["joiner"].endswith("joiner-epoch-1.onnx")


def test_wake_word_spotter_passes_tuned_score_and_threshold(monkeypatch, tmp_path):
    from tools import streaming_stt
    from tools.streaming_stt import SherpaOnnxWakeWordSpotter, WakeWordConfig

    captured = {}

    class FakeKeywordSpotter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def create_stream(self):
            return object()

    class FakeSherpa:
        KeywordSpotter = FakeKeywordSpotter

    monkeypatch.setattr(streaming_stt, "_import_sherpa_onnx", lambda: FakeSherpa)
    monkeypatch.setattr(
        streaming_stt,
        "resolve_sherpa_kws_model_files",
        lambda _cfg: {
            "encoder": "encoder.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.onnx",
            "tokens": "tokens.txt",
            "bpe_model": "bpe.model",
        },
    )
    monkeypatch.setattr(streaming_stt, "_write_wake_keywords_file", lambda _cfg, _files: str(tmp_path / "kw.txt"))

    SherpaOnnxWakeWordSpotter(WakeWordConfig(enabled=True, boost=4.0, threshold=0.21))

    assert captured["keywords_score"] == 4.0
    assert captured["keywords_threshold"] == 0.21


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
