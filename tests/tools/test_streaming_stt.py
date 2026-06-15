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


def test_sherpa_streaming_recognizer_uses_online_stream_api(monkeypatch):
    from tools import streaming_stt
    from tools.streaming_stt import SherpaOnnxStreamingRecognizer, StreamingSttConfig

    class FakeResult:
        text = "hello marvi"

    class FakeStream:
        def __init__(self):
            self.accepted = []
            self.finished = False

        def accept_waveform(self, sample_rate, samples):
            self.accepted.append((sample_rate, list(samples)))

        def input_finished(self):
            self.finished = True

    class FakeOnlineRecognizer:
        created = []

        @classmethod
        def from_transducer(cls, **kwargs):
            recognizer = cls()
            recognizer.kwargs = kwargs
            recognizer.stream = FakeStream()
            recognizer.decode_count = 0
            cls.created.append(recognizer)
            return recognizer

        def create_stream(self):
            return self.stream

        def is_ready(self, _stream):
            return self.decode_count == 0

        def decode_stream(self, _stream):
            self.decode_count += 1

        def get_result(self, _stream):
            return FakeResult()

    class FakeSherpa:
        OnlineRecognizer = FakeOnlineRecognizer

    monkeypatch.setattr(streaming_stt, "_import_sherpa_onnx", lambda: FakeSherpa)
    monkeypatch.setattr(
        streaming_stt,
        "resolve_sherpa_model_files",
        lambda _cfg: {
            "encoder": "encoder.onnx",
            "decoder": "decoder.onnx",
            "joiner": "joiner.onnx",
            "tokens": "tokens.txt",
        },
    )

    recognizer = SherpaOnnxStreamingRecognizer(StreamingSttConfig(enabled=True, sample_rate=16000))
    recognizer.start(8000)

    assert recognizer.accept_waveform([0.1, 0.2]) == "hello marvi"
    assert recognizer.finish() == "hello marvi"

    fake = FakeOnlineRecognizer.created[0]
    assert fake.stream.accepted == [(8000, [0.1, 0.2])]
    assert fake.stream.finished is True
    assert fake.decode_count == 1


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
