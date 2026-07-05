from tools.parakeet_streaming_stt import (
    DEFAULT_PARAKEET_MODEL,
    ParakeetStreamingSession,
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
