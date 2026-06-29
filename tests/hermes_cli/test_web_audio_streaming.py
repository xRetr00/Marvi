import struct
import wave


def test_write_float32_chunks_as_wav(tmp_path):
    from hermes_cli.web_server import _write_float32_chunks_as_wav

    path = tmp_path / "voice.wav"

    _write_float32_chunks_as_wav(
        [
            struct.pack("<3f", -1.0, 0.0, 1.0),
            struct.pack("<1f", 0.5),
        ],
        path,
        sample_rate=16000,
    )

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 4


def test_desktop_audio_stream_routes_are_registered():
    from hermes_cli.web_server import app

    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/api/audio/transcribe/stream" in paths
    assert "/api/audio/wake-word/stream" in paths
