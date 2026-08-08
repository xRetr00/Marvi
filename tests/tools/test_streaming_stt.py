"""Tests for local wake-word helpers."""

import json

import pytest


def test_wake_word_config_defaults_include_marvi_variants():
    from tools.streaming_stt import wake_word_config

    cfg = wake_word_config({"voice": {}})

    assert cfg.enabled is False
    assert cfg.provider == "livekit"
    assert cfg.model == "livekit-marvi"
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
                    "debug": True,
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
    assert cfg.debug is True
    assert cfg.command_timeout_ms == 9000
    assert cfg.cooldown_ms == 500


def test_wake_word_config_defaults_livekit_model_for_livekit_provider():
    from tools.streaming_stt import DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID, wake_word_config

    cfg = wake_word_config({"voice": {"wake_word": {"enabled": True, "provider": "livekit"}}})

    assert cfg.provider == "livekit"
    assert cfg.model == DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID


def test_wake_word_config_migrates_legacy_sherpa_values_to_livekit():
    from tools.streaming_stt import DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID, wake_word_config

    cfg = wake_word_config(
        {"voice": {"wake_word": {"enabled": True, "provider": "sherpa_onnx", "model": "kws-en-3.3m"}}}
    )

    assert (cfg.provider, cfg.model) == ("livekit", DEFAULT_LIVEKIT_WAKE_WORD_MODEL_ID)


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


def test_wake_word_factory_returns_livekit_spotter():
    from tools.streaming_stt import WakeWordFactory

    class FakeSpotter:
        pass

    spotter = FakeSpotter()
    factory = WakeWordFactory(create_livekit_spotter=lambda _cfg: spotter)

    assert factory.create({"voice": {"wake_word": {"enabled": True}}}) is spotter


def test_wake_word_factory_dispatches_livekit_provider():
    from tools.streaming_stt import WakeWordFactory

    class FakeSpotter:
        pass

    spotter = FakeSpotter()
    factory = WakeWordFactory(create_livekit_spotter=lambda cfg: spotter if cfg.provider == "livekit" else None)

    assert factory.create({"voice": {"wake_word": {"enabled": True, "provider": "livekit"}}}) is spotter


def test_livekit_spotter_loads_onnx_models_from_directory(monkeypatch, tmp_path):
    from tools import streaming_stt
    from tools.streaming_stt import LiveKitWakeWordSpotter, WakeWordConfig

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "hey_marvi.onnx").write_text("x", encoding="utf-8")
    (model_dir / "marvy.onnx").write_text("x", encoding="utf-8")
    captured = {}

    class FakeWakeWordModel:
        def __init__(self, models):
            captured["models"] = models

        def predict(self, _samples):
            return {"hey_marvi": 0.2, "marvy": 0.8}

    monkeypatch.setattr(streaming_stt, "_import_livekit_wakeword_model", lambda: FakeWakeWordModel)
    spotter = LiveKitWakeWordSpotter(WakeWordConfig(enabled=True, provider="livekit", model=str(model_dir), threshold=0.5))

    assert [path.name for path in captured["models"]] == ["hey_marvi.onnx", "marvy.onnx"]
    assert spotter.accept_waveform([0.1, 0.2]) == ""
    assert spotter.accept_waveform([0.1] * 32000) == "marvy"


def test_livekit_spotter_writes_debug_score_telemetry(monkeypatch, tmp_path):
    from tools import streaming_stt
    from tools.streaming_stt import LiveKitWakeWordSpotter, WakeWordConfig

    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "marvi.onnx").write_text("x", encoding="utf-8")
    scores = [{"marvi": 0.4, "marvy": 0.3}, {"marvi": 0.9, "marvy": 0.2}]

    class FakeWakeWordModel:
        def __init__(self, models):
            self.models = models

        def predict(self, _samples):
            return scores.pop(0)

    monkeypatch.setattr(streaming_stt, "_import_livekit_wakeword_model", lambda: FakeWakeWordModel)
    monkeypatch.setattr(streaming_stt, "get_hermes_home", lambda: tmp_path)
    spotter = LiveKitWakeWordSpotter(
        WakeWordConfig(enabled=True, provider="livekit", model=str(model_dir), threshold=0.5, debug=True)
    )

    assert spotter.accept_waveform([0.1] * 32000) == ""
    assert spotter.accept_waveform([0.1] * 32000) == "marvi"

    log_path = tmp_path / "logs" / "wakeword-livekit.jsonl"
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [event["decision"] for event in events] == ["ignored", "passed"]
    assert events[0]["scores"]["marvi"] == 0.4
    assert events[1]["label"] == "marvi"
    assert events[1]["threshold"] == 0.5


def test_wakeword_debug_telemetry_rotates_at_bounded_size(monkeypatch, tmp_path):
    from tools import streaming_stt

    monkeypatch.setattr(streaming_stt, "_WAKEWORD_TELEMETRY_MAX_BYTES", 16)
    path = tmp_path / "wakeword-livekit.jsonl"

    streaming_stt._append_wakeword_telemetry(path, "first-event")
    streaming_stt._append_wakeword_telemetry(path, "second-event")

    assert path.read_text(encoding="utf-8") == "second-event\n"
    assert path.with_name(f"{path.name}.1").read_text(encoding="utf-8") == "first-event\n"


def test_wakeword_debug_drops_legacy_oversized_log(monkeypatch, tmp_path):
    from tools import streaming_stt

    monkeypatch.setattr(streaming_stt, "_WAKEWORD_TELEMETRY_MAX_BYTES", 16)
    path = tmp_path / "wakeword-livekit.jsonl"
    path.write_text("legacy-unbounded-debug-data", encoding="utf-8")

    streaming_stt._append_wakeword_telemetry(path, "new-event")

    assert path.read_text(encoding="utf-8") == "new-event\n"
    assert not path.with_name(f"{path.name}.1").exists()


def test_wake_word_factory_rejects_disabled_config():
    from tools.streaming_stt import WakeWordUnavailable, WakeWordFactory

    factory = WakeWordFactory(create_livekit_spotter=lambda _cfg: object())

    with pytest.raises(WakeWordUnavailable, match="disabled"):
        factory.create({"voice": {"wake_word": {"enabled": False}}})


def test_wake_word_factory_accepts_resolved_config():
    from tools.streaming_stt import WakeWordConfig, WakeWordFactory

    factory = WakeWordFactory(create_livekit_spotter=lambda cfg: cfg)
    cfg = WakeWordConfig(enabled=True)

    assert factory.create(cfg) is cfg


def test_sherpa_native_self_test_reports_child_process_failure(monkeypatch):
    from types import SimpleNamespace

    from tools import streaming_stt
    from tools.streaming_stt import WakeWordConfig, WakeWordUnavailable

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=3221225477,
            stdout="",
            stderr="The requested API version [24] is not available. Current ORT Version is: 1.17.1",
        )

    monkeypatch.setattr(streaming_stt.subprocess, "run", fake_run)

    with pytest.raises(WakeWordUnavailable, match="Current ORT Version is: 1.17.1"):
        streaming_stt._run_sherpa_native_self_test(WakeWordConfig(enabled=True))


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
