import sys
import types

import numpy as np

from tools.parakeet_streaming_stt import (
    DEFAULT_PARAKEET_MODEL,
    ParakeetStreamingConfig,
    ParakeetStreamingSession,
    _NativeParakeetModel,
    _NativeParakeetStream,
    _load_parakeet_model,
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


def test_resolve_parakeet_config_defaults_to_batch_engine():
    cfg = resolve_parakeet_config({"streaming": {"provider": "parakeet"}})

    assert cfg.engine == "batch"


def test_resolve_parakeet_config_accepts_native_engine():
    cfg = resolve_parakeet_config(
        {"streaming": {"provider": "parakeet", "parakeet": {"engine": "native"}}}
    )

    assert cfg.engine == "native"


def test_native_stream_accumulates_incremental_text_and_distinguishes_eou():
    pieces = iter(["hello", " world", ""])

    class FakeLib:
        def parakeet_capi_stream_feed(self, _stream, _pcm, _count, events):
            events._obj.value = 1 if len(model.seen) == 1 else 0
            model.seen.append("feed")
            return 1

        def parakeet_capi_stream_finalize(self, _stream):
            return 1

        def parakeet_capi_stream_free(self, _stream):
            model.freed += 1

    class FakeNativeModel:
        def __init__(self):
            self.lib = FakeLib()
            self.seen = []
            self.freed = 0

        def read_string(self, _pointer):
            return next(pieces)

    model = FakeNativeModel()
    stream = _NativeParakeetStream(model, 123)
    samples = np.zeros(1600, dtype=np.float32)

    assert stream.push(samples) == ("hello", 0.0)
    assert stream.push(samples) == ("hello world", 1.0)
    assert stream.finish() == "hello world"
    assert model.freed == 1


def test_native_session_suppresses_model_text_for_effective_silence():
    stream = object.__new__(_NativeParakeetStream)
    stream.push = lambda _samples: ("yeah", 1.0)
    stream.finish = lambda: "yeah"
    model = object.__new__(_NativeParakeetModel)
    model.begin_stream = lambda: stream
    session = ParakeetStreamingSession(
        {"streaming": {"parakeet": {"engine": "native"}}},
        loader=lambda _config: model,
    )
    session.start()

    assert session.accept_bytes(np.zeros(1600, dtype=np.float32).tobytes()) == ""
    assert session.finish() == ""


def test_load_parakeet_model_requires_driver_update_on_cuda_driver_error(monkeypatch):
    import tools.parakeet_streaming_stt as mod
    import pytest

    devices = []

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

    class FakeParakeetModel:
        def to(self, device):
            devices.append(device)
            if device == "cuda":
                raise RuntimeError("cudaErrorInsufficientDriver: CUDA driver version is insufficient")
            return self

        def eval(self):
            return None

    class FakeASRModel:
        @staticmethod
        def from_pretrained(model_name):
            return FakeParakeetModel()

    nemo_pkg = types.ModuleType("nemo")
    collections_pkg = types.ModuleType("nemo.collections")
    asr_pkg = types.ModuleType("nemo.collections.asr")
    asr_pkg.models = types.SimpleNamespace(ASRModel=FakeASRModel)
    nemo_pkg.collections = collections_pkg
    collections_pkg.asr = asr_pkg

    mod._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=FakeCuda()))
    monkeypatch.setitem(sys.modules, "nemo", nemo_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections", collections_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", asr_pkg)

    with pytest.raises(RuntimeError, match="Update the NVIDIA GPU driver"):
        _load_parakeet_model(ParakeetStreamingConfig(device="cuda", cpu_fallback=True))

    assert devices == ["cuda"]


def test_resolve_parakeet_config_min_free_vram_default_and_override():
    cfg = resolve_parakeet_config({"streaming": {"provider": "parakeet"}})
    assert cfg.min_free_vram_mb == 2048

    cfg = resolve_parakeet_config(
        {"streaming": {"provider": "parakeet", "parakeet": {"min_free_vram_mb": "512"}}}
    )
    assert cfg.min_free_vram_mb == 512


def _install_fake_nemo(monkeypatch, devices):
    import types as _types

    class FakeParakeetModel:
        def to(self, device):
            devices.append(device)
            return self

        def eval(self):
            return None

    class FakeASRModel:
        @staticmethod
        def from_pretrained(model_name):
            return FakeParakeetModel()

    nemo_pkg = _types.ModuleType("nemo")
    collections_pkg = _types.ModuleType("nemo.collections")
    asr_pkg = _types.ModuleType("nemo.collections.asr")
    asr_pkg.models = types.SimpleNamespace(ASRModel=FakeASRModel)
    nemo_pkg.collections = collections_pkg
    collections_pkg.asr = asr_pkg

    monkeypatch.setitem(sys.modules, "nemo", nemo_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections", collections_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", asr_pkg)


def test_vram_preflight_falls_back_to_cpu_when_free_vram_low(monkeypatch, caplog):
    import logging
    import tools.parakeet_streaming_stt as mod

    devices = []
    _install_fake_nemo(monkeypatch, devices)

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def mem_get_info():
            # 100MB free — well below the 2048MB default threshold.
            return (100 * 1024 * 1024, 8 * 1024 * 1024 * 1024)

    mod._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=FakeCuda()))

    caplog.set_level(logging.WARNING, logger="tools.parakeet_streaming_stt")
    _load_parakeet_model(ParakeetStreamingConfig(device="cuda", cpu_fallback=True))

    # Preflight rerouted the load to CPU without ever attempting CUDA.
    assert devices == ["cpu"]
    assert any("free VRAM" in r.getMessage() for r in caplog.records)
    mod._MODEL_CACHE.clear()


def test_vram_preflight_proceeds_on_cuda_when_free_vram_sufficient(monkeypatch):
    import tools.parakeet_streaming_stt as mod

    devices = []
    _install_fake_nemo(monkeypatch, devices)

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def mem_get_info():
            # 6GB free — comfortably above the threshold.
            return (6 * 1024 * 1024 * 1024, 8 * 1024 * 1024 * 1024)

    mod._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=FakeCuda()))

    _load_parakeet_model(ParakeetStreamingConfig(device="cuda", cpu_fallback=True))

    assert devices == ["cuda"]
    mod._MODEL_CACHE.clear()


def test_vram_preflight_unknown_free_vram_proceeds_as_today(monkeypatch):
    # mem_get_info raising means "unknown" — the load proceeds on CUDA
    # exactly as it did before the preflight existed.
    import tools.parakeet_streaming_stt as mod

    devices = []
    _install_fake_nemo(monkeypatch, devices)

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def mem_get_info():
            raise RuntimeError("driver hiccup")

    mod._MODEL_CACHE.clear()
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=FakeCuda()))

    _load_parakeet_model(ParakeetStreamingConfig(device="cuda", cpu_fallback=True))

    assert devices == ["cuda"]
    mod._MODEL_CACHE.clear()


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
        {"streaming": {"provider": "parakeet", "engine": "cache_aware"}},
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
    # Even with cache_aware requested, a model lacking the API -> no stream.
    session = ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet", "engine": "cache_aware"}},
        loader=lambda _cfg: FakeModel(),
        temp_dir=tmp_path,
    )
    session.start()
    assert session._stream is None


def test_batch_engine_has_no_partials(tmp_path):
    # engine=batch buffers only during listening and transcribes once at finish
    # -- keeps the GPU free for the wake word + TTS (the production default).
    session = ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet", "engine": "batch"}},
        loader=lambda _cfg: FakeModel(),
        temp_dir=tmp_path,
    )
    session.start()
    assert session._stream is None
    big = np.ones(ParakeetStreamingSession._PARTIAL_INTERVAL_SAMPLES * 2, dtype=np.float32).tobytes()
    assert session.accept_bytes(big) == ""  # no live partial in batch mode
    assert session.finish() == "hello marvi"  # transcribed once at the end


def test_batch_engine_probe_decodes_once_and_finish_reuses_it(tmp_path):
    fake_model = FakeModel()
    session = ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet"}},
        loader=lambda _cfg: fake_model,
        temp_dir=tmp_path,
    )
    session.start()
    frame = np.ones(16000, dtype=np.float32).tobytes()
    assert session.accept_bytes(frame) == ""

    assert session.probe() == "hello marvi"
    assert session.last_eou is True
    assert session.finish() == "hello marvi"
    assert len(fake_model.calls) == 1


def test_parakeet_streaming_session_emits_partials_and_eou(tmp_path):
    import numpy as np

    # Live partials are opt-in via engine=rebuffer.
    session = ParakeetStreamingSession(
        {"streaming": {"provider": "parakeet", "engine": "rebuffer"}},
        loader=lambda _cfg: FakeModel(),
        temp_dir=tmp_path,
    )
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

        def probe(self):
            print("[NeMo I noisy probe line]")
            self.last_eou = True
            self.last_eou_prob = 1.0
            return "hello"

        def finish(self):
            print("[NeMo I noisy final line]")
            return "hello"

    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr("tools.parakeet_streaming_stt.ParakeetStreamingSession", NoisySession)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"type":"start","stt_config":{}}\n{"type":"audio","data":""}\n{"type":"probe"}\n{"type":"stop"}\n'))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    assert _run_stdio_server() == 0
    lines = stdout.getvalue().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["ready", "partial", "probe", "final"]
    assert "[NeMo" not in stdout.getvalue()
    assert "[NeMo" in stderr.getvalue()
