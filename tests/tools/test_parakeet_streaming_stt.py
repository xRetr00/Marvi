import numpy as np

from tools.parakeet_streaming_stt import (
    DEFAULT_PARAKEET_MODEL,
    ParakeetStreamingSession,
    _run_stdio_server,
    resolve_parakeet_config,
)


class FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, paths, **kwargs):
        self.calls.append((paths, kwargs))
        return ["hello marvi <EOU>"]


def test_resolve_parakeet_config_uses_streaming_overrides():
    cfg = resolve_parakeet_config(
        {
            "streaming": {
                "provider": "parakeet",
                "parakeet": {"model": "custom/model"},
                "dtype": "float16",
            }
        }
    )

    assert cfg.model == "custom/model"
    assert cfg.dtype == "float16"


def test_resolve_parakeet_config_uses_nested_memory_limits():
    cfg = resolve_parakeet_config(
        {
            "streaming": {
                "provider": "parakeet",
                "parakeet": {
                    "cpu_fallback": "false",
                    "max_gpu_memory_gb": "2.5",
                },
            }
        }
    )

    assert cfg.cpu_fallback is False
    assert cfg.max_gpu_memory_gb == 2.5


def test_resolve_parakeet_config_ignores_whisper_model_name():
    cfg = resolve_parakeet_config(
        {
            "streaming": {
                "provider": "parakeet",
                "model": "large-v3-turbo",
            }
        }
    )

    assert cfg.model == DEFAULT_PARAKEET_MODEL


def test_parakeet_streaming_session_transcribes_buffered_chunks(tmp_path):
    fake_model = FakeModel()

    def fake_loader(_cfg):
        return fake_model

    session = ParakeetStreamingSession({"streaming": {"provider": "parakeet"}}, loader=fake_loader, temp_dir=tmp_path)
    session.start()
    session.accept_samples([0.1, 0.2, 0.3, 0.4])
    session.accept_samples([0.5, 0.6, 0.7])

    assert session.finish() == "hello marvi"
    assert fake_model.calls
    assert fake_model.calls[0][1]["batch_size"] == 1


def test_parakeet_streaming_session_returns_empty_without_audio(tmp_path):
    session = ParakeetStreamingSession({}, loader=lambda _cfg: FakeModel(), temp_dir=tmp_path)
    session.start()

    assert session.finish() == ""


class _FakeStreamingModel:
    """Model exposing the cache-aware API surface so the session picks the fast path."""

    def __init__(self):
        self.encoder = self  # get_initial_cache_state lives here in the fake

    def get_initial_cache_state(self, batch_size=1):
        return (None, None, None)

    def conformer_stream_step(self, **kwargs):  # pragma: no cover - not called in test
        raise AssertionError("real streaming step should be stubbed out in tests")


def test_session_uses_cache_aware_engine_when_available(tmp_path, monkeypatch):
    import tools.parakeet_streaming_stt as mod

    pushes = []

    class FakeStream:
        def __init__(self, model, config):
            self.model = model

        def push(self, samples):
            pushes.append(len(samples))
            # First push buffers (None), second returns a partial + EOU.
            return ("streamed text", 1.0) if len(pushes) >= 2 else None

        def finish(self):
            return "streamed final"

    monkeypatch.setattr(mod, "_CacheAwareStream", FakeStream)

    session = mod.ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet"}},
        loader=lambda _cfg: _FakeStreamingModel(),
        temp_dir=tmp_path,
    )
    session.start()
    assert session._stream is not None  # fast path selected

    frame = np.zeros(100, dtype=np.float32).tobytes()
    assert session.accept_bytes(frame) == ""  # buffering
    assert session.accept_bytes(frame) == "streamed text"
    assert session.last_eou is True
    assert session.last_eou_prob == 1.0
    assert session.finish() == "streamed final"


def test_session_falls_back_when_model_lacks_streaming_api(tmp_path):
    # FakeModel has only .transcribe -> no cache-aware API -> re-transcribe engine.
    session = ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet"}},
        loader=lambda _cfg: FakeModel(),
        temp_dir=tmp_path,
    )
    session.start()
    assert session._stream is None


def test_parakeet_streaming_session_emits_partials_and_eou(tmp_path):
    import numpy as np

    session = ParakeetStreamingSession({}, loader=lambda _cfg: FakeModel(), temp_dir=tmp_path)
    session.start()

    interval = ParakeetStreamingSession._PARTIAL_INTERVAL_SAMPLES
    silent = np.zeros(interval // 2, dtype=np.float32).tobytes()

    # Below one interval of audio: buffer only, no partial yet.
    assert session.accept_bytes(silent) == ""
    assert session.last_eou is False

    # Crossing the interval yields a live partial and surfaces the <EOU> flag.
    assert session.accept_bytes(silent) == "hello marvi"
    assert session.last_eou is True


def test_stdio_server_keeps_stdout_json_only_when_model_logs(monkeypatch):
    import io
    import json
    import sys

    class NoisySession:
        last_eou = False

        def __init__(self, _cfg):
            pass

        def start(self):
            print("[NeMo W noisy startup line]")

        def accept_bytes(self, _raw):
            print("[NeMo I noisy transcribe line]")
            return "hello"

        def finish(self):
            print("[NeMo I noisy final line]")
            return "hello"

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr("tools.parakeet_streaming_stt.ParakeetStreamingSession", NoisySession)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"type":"start","stt_config":{}}\n{"type":"audio","data":""}\n{"type":"stop"}\n'))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    assert _run_stdio_server() == 0
    lines = stdout.getvalue().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["ready", "partial", "final"]
    assert "[NeMo" not in stdout.getvalue()
    assert "[NeMo" in stderr.getvalue()
