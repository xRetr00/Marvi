import queue

from tools.nemotron_streaming_stt import NemotronStreamingSession, resolve_nemotron_config


class FakeInputs(dict):
    @property
    def input_features(self):
        return self["input_features"]

    def to(self, **_kwargs):
        return self


class FakeProcessor:
    tokenizer = object()
    num_samples_first_audio_chunk = 4
    num_samples_per_audio_chunk = 2
    num_mel_frames_first_audio_chunk = 1

    def __call__(self, audio, **kwargs):
        return FakeInputs(input_features=FakeFeatures(len(audio)), flags=kwargs)


class FakeFeatures:
    def __init__(self, size):
        self.size = size

    def __getitem__(self, _key):
        return self


class FakeStreamer:
    def __init__(self, *_args, **_kwargs):
        self.items = queue.Queue()

    def __iter__(self):
        while True:
            item = self.items.get(timeout=5)
            if item is None:
                return
            yield item


class FakeModel:
    device = "cpu"
    dtype = None

    def generate(self, **kwargs):
        assert "input_features" in kwargs
        for _features in kwargs["input_features"]:
            pass
        kwargs["streamer"].items.put("hello marvi")
        kwargs["streamer"].items.put(None)


def test_resolve_nemotron_config_uses_streaming_overrides():
    cfg = resolve_nemotron_config(
        {
            "streaming": {
                "provider": "nemotron",
                "nemotron": {"model": "custom/model"},
                "lookahead_tokens": 6,
                "dtype": "float16",
            }
        }
    )

    assert cfg.model == "custom/model"
    assert cfg.lookahead_tokens == 6
    assert cfg.dtype == "float16"


def test_resolve_nemotron_config_ignores_whisperlive_model_name():
    cfg = resolve_nemotron_config(
        {
            "streaming": {
                "provider": "nemotron",
                "model": "large-v3-turbo",
                "nemotron": {"lookahead_tokens": 2},
            }
        }
    )

    assert cfg.model == "nvidia/nemotron-speech-streaming-en-0.6b"
    assert cfg.lookahead_tokens == 2


def test_nemotron_streaming_session_feeds_chunks_to_generator():
    def fake_loader(_cfg):
        return FakeProcessor(), FakeModel(), FakeStreamer

    session = NemotronStreamingSession({"streaming": {"provider": "nemotron"}}, loader=fake_loader)
    session.start()
    session.accept_samples([0.1, 0.2, 0.3, 0.4])
    session.accept_samples([0.5, 0.6, 0.7])

    assert session.finish() == "hello marvi"


def test_nemotron_streaming_session_returns_empty_without_audio():
    session = NemotronStreamingSession({}, loader=lambda _cfg: (FakeProcessor(), FakeModel(), FakeStreamer))
    session.start()

    assert session.finish() == ""
