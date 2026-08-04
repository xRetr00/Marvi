"""Protocol-level tests for WS /api/voice/duplex.

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern from
tests/hermes_cli/test_web_server_console_ws.py. STT, the instant lane,
speaker identify, TTS, and the escalation deep-task runner are ALL faked at
the ``hermes_cli.web_server`` module-boundary seam functions
(``_duplex_stt_session``, ``_duplex_identify_speaker``,
``_duplex_stream_instant_reply``, ``_duplex_stream_tts_chunks``,
``_duplex_make_vad_gate``, ``_duplex_run_deep_task``) -- no real
sherpa-onnx/Parakeet/TTS/LLM/network is touched.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import threading
import time

import pytest

from hermes_cli import web_server


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSttSession:
    """Drives ``_DuplexSession``'s listening state.

    Test controls queue up (partial_text, eou) pairs consumed one per
    ``accept_bytes`` call; ``finish()`` returns whatever ``final_text`` is
    set to at the moment it's called.
    """

    def __init__(self):
        self.last_eou = False
        self.last_eou_prob = 0.0
        self.begin_count = 0
        self.closed = False
        self.accepted_chunks: list[bytes] = []
        self._responses: list[tuple[str, bool]] = []
        self.final_text = ""

    def queue_response(self, partial: str, eou: bool) -> None:
        self._responses.append((partial, eou))

    def begin(self) -> None:
        self.begin_count += 1
        self.last_eou = False
        self.last_eou_prob = 0.0

    def accept_bytes(self, chunk: bytes) -> str:
        self.accepted_chunks.append(chunk)
        if self._responses:
            partial, eou = self._responses.pop(0)
        else:
            partial, eou = "", False
        self.last_eou = eou
        self.last_eou_prob = 1.0 if eou else 0.0
        return partial

    def finish(self) -> str:
        return self.final_text

    def running(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class FakeVadGate:
    """Test flips ``speaking`` to simulate sustained speech for barge-in."""

    def __init__(self):
        self.speaking = False
        self.accepted: list[list[float]] = []

    def accept(self, samples) -> bool:
        self.accepted.append(list(samples))
        return self.speaking

    def has_recent_speech(self, within_ms: int = 1200) -> bool:
        return self.speaking


def _pcm16_chunk(n_samples: int = 320, value: int = 100) -> bytes:
    """~20ms of 16kHz mono PCM16 at a constant sample value."""
    import struct

    return struct.pack(f"<{n_samples}h", *([value] * n_samples))


def _audio_msg(chunk: bytes) -> dict:
    return {"type": "audio", "data": base64.b64encode(chunk).decode("ascii")}


def _duplex_url() -> str:
    from urllib.parse import urlencode

    return f"/api/voice/duplex?{urlencode({'token': web_server._SESSION_TOKEN})}"


def _recv_until(conn, frame_type: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = conn.receive_json()
        if frame.get("type") == frame_type:
            return frame
    raise AssertionError(f"Timed out waiting for {frame_type!r} frame")


def _drain_until(conn, frame_types, *, timeout: float = 5.0) -> list[dict]:
    """Collect frames until one matching any of ``frame_types`` is seen
    (inclusive); returns everything collected in order."""
    deadline = time.monotonic() + timeout
    out = []
    while time.monotonic() < deadline:
        frame = conn.receive_json()
        out.append(frame)
        if frame.get("type") in frame_types:
            return out
    raise AssertionError(f"Timed out waiting for one of {frame_types!r}; got {out!r}")


def _finish_playback(conn, *, timeout: float = 5.0) -> list[dict]:
    frames = _drain_until(conn, {"tts_end"}, timeout=timeout)
    conn.send_json({"type": "playback_done"})
    return frames


def _recv_with_playback_acks(conn, frame_type: str, *, timeout: float = 10.0) -> dict:
    """Receive until frame_type while acknowledging every completed TTS cycle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = conn.receive_json()
        if frame.get("type") == "tts_end":
            conn.send_json({"type": "playback_done"})
        if frame.get("type") == frame_type:
            return frame
    raise AssertionError(f"Timed out waiting for {frame_type!r}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def duplex_client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    previous_bound_host = getattr(web_server.app.state, "bound_host", None)
    previous_transcribe_lock = getattr(web_server.app.state, "audio_transcribe_lock", None)
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = None
    # Fresh, full-capacity semaphore per test. TestClient (used here without
    # a `with` block, so the app lifespan never runs) spins a NEW anyio
    # portal/event loop per `websocket_connect()` call; asyncio.Semaphore
    # only binds to "a" loop the first time it actually has to wait (value
    # hits 0), so reusing one semaphore instance across tests/connections on
    # different loops throws "bound to a different event loop" the moment
    # it's exhausted -- and since app.state is a module-level singleton that
    # outlives any one test, a previous test's session.close() racing with
    # TestClient teardown (a known async-fire-and-forget gap, unrelated to
    # this semaphore) can otherwise leave a permit "leaked" into the next
    # test. A fresh semaphore per test sidesteps both: never exhausted
    # within a single test's 1-2 connections, so acquire() never needs to
    # wait/bind to a loop at all.
    web_server.app.state.audio_transcribe_lock = asyncio.Semaphore(web_server._STREAMING_STT_MAX_CONCURRENT)
    # TTS self-enrollment (spec Part 1.3) fires a background thread from
    # _DuplexSession.start() that would otherwise touch the real TTS/sherpa
    # stack -- neuter it for every duplex_client test; barge-in negative-
    # screen tests that care about TTS-echo matching use the `tts_echo`
    # seam fixture directly instead.
    monkeypatch.setattr(web_server._DuplexSession, "_start_tts_self_enrollment", lambda self: None)

    client = TestClient(web_server.app)
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
        if previous_auth_required is None:
            if hasattr(web_server.app.state, "auth_required"):
                delattr(web_server.app.state, "auth_required")
        else:
            web_server.app.state.auth_required = previous_auth_required
        if previous_bound_host is None:
            if hasattr(web_server.app.state, "bound_host"):
                delattr(web_server.app.state, "bound_host")
        else:
            web_server.app.state.bound_host = previous_bound_host
        if previous_transcribe_lock is None:
            if hasattr(web_server.app.state, "audio_transcribe_lock"):
                delattr(web_server.app.state, "audio_transcribe_lock")
        else:
            web_server.app.state.audio_transcribe_lock = previous_transcribe_lock


@pytest.fixture
def stt_session(monkeypatch):
    session = FakeSttSession()
    monkeypatch.setattr(web_server, "_duplex_stt_session", lambda stt_cfg: session)
    return session


@pytest.fixture
def identify_speaker(monkeypatch):
    state = {"label": "owner", "score": 0.9, "name": "Shereef"}

    def fake_identify(pcm16_bytes):
        return state["label"], state["score"], state["name"]

    monkeypatch.setattr(web_server, "_duplex_identify_speaker", fake_identify)
    return state


@pytest.fixture
def instant_reply(monkeypatch):
    state = {
        "deltas": ["Hi", " there."], "raises": None, "delay": 0.0, "activity": None,
        "warm_status": {"hit": True, "construct_ms": None}, "warm_calls": [],
    }

    def fake_stream(
        transcript, utterance, *, allow_escalation, activity_callback=None, warm_status_callback=None,
    ):
        if warm_status_callback is not None:
            state["warm_calls"].append(state["warm_status"])
            warm_status_callback(state["warm_status"])
        if state["raises"] is not None:
            raise state["raises"]
        if state["activity"] and activity_callback:
            activity_callback({"status": "started", **state["activity"]})
            activity_callback({"status": "completed", **state["activity"]})
        for d in state["deltas"]:
            if state["delay"]:
                time.sleep(state["delay"])
            yield d

    monkeypatch.setattr(web_server, "_duplex_stream_instant_reply", fake_stream)
    return state


@pytest.fixture
def tts_chunks(monkeypatch):
    calls: list[str] = []

    def fake_stream(text):
        calls.append(text)
        return [
            {"type": "start", "sample_rate": 24000, "provider": "fake"},
            {"type": "chunk", "audio": "AAA="},
            {"type": "end", "provider": "fake"},
        ]

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", fake_stream)
    return calls


@pytest.fixture
def vad_gate(monkeypatch):
    gate = FakeVadGate()
    gate.speaking = True
    monkeypatch.setattr(web_server, "_duplex_make_vad_gate", lambda: gate)
    return gate


@pytest.fixture
def focus_mode(monkeypatch):
    """Controls the ``tools.voice_speaker_id.focus_mode_active`` seam.

    Defaults to inactive -- matching a bare install with nothing enrolled
    -- so every pre-existing test is unaffected unless it opts in.
    """
    state = {"active": False}

    def fake_active(cfg):
        return state["active"]

    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", fake_active)
    return state


@pytest.fixture
def identify_zoned(monkeypatch):
    """Controls the ``_duplex_identify_speaker_zoned`` seam (fail-open
    three-zone voice focus, spec §4 2026-07-15 round). Defaults to a
    confident OWNER match with plenty of clean audio -- so tests that only
    care about "focus off"/unrelated behavior are unaffected unless they opt
    into a different zone.
    """
    state = {
        "zone": "OWNER", "label": "owner", "score": 0.9, "audio_ms": 3000,
        "name": "Shereef", "model_mismatch": False, "embedding": [0.9, 0.1],
    }

    def fake_zoned(pcm16_bytes, cfg):
        return dict(state)

    monkeypatch.setattr(web_server, "_duplex_identify_speaker_zoned", fake_zoned)
    return state


@pytest.fixture
def tts_echo(monkeypatch):
    """Controls the ``_duplex_tts_echo_score`` seam (barge-in confirm-
    negative TTS self-echo check). Defaults to "never matches"."""
    state = {"score": -1.0}

    def fake_score(pcm16_bytes, cfg):
        return state["score"]

    monkeypatch.setattr(web_server, "_duplex_tts_echo_score", fake_score)
    return state


@pytest.fixture
def speaker_id_windows(monkeypatch):
    """Fixed owner_threshold/competing_window_seconds/continuity_seconds so
    zone/window tests don't depend on real config resolution."""
    monkeypatch.setattr(web_server, "_duplex_owner_threshold", lambda cfg: 0.45)
    monkeypatch.setattr(web_server, "_duplex_competing_window_seconds", lambda cfg: 90.0)
    monkeypatch.setattr(web_server, "_duplex_continuity_seconds", lambda cfg: 120.0)


@pytest.fixture
def deep_task(monkeypatch):
    state = {"text": "Deep answer.", "raises": None, "calls": [], "modes": [], "delay": 0.0}

    def fake_run(transcript_messages, task_text, *, mode="thinking", activity_callback=None):
        state["calls"].append(task_text)
        state["modes"].append(mode)
        if state["delay"]:
            time.sleep(state["delay"])
        if state["raises"] is not None:
            raise state["raises"]
        return state["text"]

    monkeypatch.setattr(web_server, "_duplex_run_deep_task", fake_run)
    return state


@pytest.fixture
def warm_lane(monkeypatch):
    state = {
        "calls": [],
        "result": {"ok": True, "construct_ms": 12.5, "provider": "fake-provider", "model": "fake-model"},
    }

    def fake_warm(transcript, cfg):
        state["calls"].append({"transcript": transcript, "cfg": cfg})
        return state["result"]

    monkeypatch.setattr(web_server, "_duplex_warm_instant_lane", fake_warm)
    return state


@pytest.fixture
def full_fakes(
    stt_session, identify_speaker, identify_zoned, tts_echo, speaker_id_windows,
    instant_reply, tts_chunks, vad_gate, deep_task, warm_lane, focus_mode,
):
    return {
        "stt": stt_session,
        "identify": identify_speaker,
        "zoned": identify_zoned,
        "tts_echo": tts_echo,
        "instant": instant_reply,
        "tts": tts_chunks,
        "vad": vad_gate,
        "deep": deep_task,
        "warm": warm_lane,
        "focus": focus_mode,
    }


# ---------------------------------------------------------------------------
# Basic protocol / routing
# ---------------------------------------------------------------------------


def test_route_is_registered():
    paths = {getattr(r, "path", "") for r in web_server.app.routes}
    assert "/api/voice/duplex" in paths


def test_speaker_enrollment_api(duplex_client, monkeypatch):
    from tools import voice_speaker_id

    speakers = []

    def fake_enroll(name, pcm):
        assert pcm == b"\x01\x02"
        speakers[:] = [{"name": name, "is_owner": True, "embeddings": 1}]

    monkeypatch.setattr(voice_speaker_id, "enroll", fake_enroll)
    monkeypatch.setattr(voice_speaker_id, "list_speakers", lambda: list(speakers))
    monkeypatch.setattr(voice_speaker_id, "remove_speaker", lambda name: bool(speakers.pop()) if speakers else False)

    response = duplex_client.post(
        "/api/voice/speakers",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
        json={"name": "Owner", "audio": [base64.b64encode(b"\x01\x02").decode("ascii")]},
    )
    assert response.status_code == 200
    assert response.json()["speakers"][0]["is_owner"] is True

    response = duplex_client.delete(
        "/api/voice/speakers/Owner",
        headers={"Authorization": f"Bearer {web_server._SESSION_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json() == {"speakers": []}


def test_ready_on_connect(duplex_client, full_fakes):
    with duplex_client.websocket_connect(_duplex_url()) as conn:
        assert conn.receive_json() == {"type": "ready"}


def test_moonshine_pause_waits_for_smart_turn_and_commit_silence(monkeypatch):
    from tools import semantic_turn

    predictions = iter((False, True))
    monkeypatch.setattr(semantic_turn, "pipecat_smart_turn_complete", lambda chunks, rate: next(predictions))

    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(
            FakeWs(),
            {"stt": {"streaming": {"provider": "moonshine"}}, "voice": {"semantic_turn": True}},
        )
        session.stt_session = FakeSttSession()
        session.stt_session.queue_response("not finished", True)
        session.stt_session.queue_response("now finished", True)
        finalized = 0

        async def finalize():
            nonlocal finalized
            finalized += 1

        session._finalize_utterance = finalize
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 0
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 0
        gate = FakeVadGate()
        gate.speaking = True
        session.turn_vad_gate = gate
        session._turn_vad_unavailable = False
        await session._feed_stt(_pcm16_chunk())
        assert session._smart_turn_accepted_at is None
        gate.speaking = False
        session._smart_turn_accepted_at = time.monotonic() - 1.0
        session.cfg["voice"]["smart_turn_commit_delay_ms"] = 250
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 1

    asyncio.run(run())


def test_ten_vad_finishes_after_smart_turn_rejects_and_silence_continues(monkeypatch):
    from tools import semantic_turn

    monkeypatch.setattr(semantic_turn, "pipecat_smart_turn_complete", lambda chunks, rate: False)

    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(
            FakeWs(),
            {
                "stt": {"streaming": {"provider": "moonshine"}},
                "voice": {"semantic_turn": True, "smart_turn_vad_fallback_ms": 250},
            },
        )
        session.stt_session = FakeSttSession()
        gate = FakeVadGate()
        session.turn_vad_gate = gate
        gate.speaking = True
        session.stt_session.queue_response("unfinished", True)
        finalized = 0

        async def finalize():
            nonlocal finalized
            finalized += 1

        session._finalize_utterance = finalize
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 0

        gate.speaking = False
        session._smart_turn_rejected_at = time.monotonic() - 1.0
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 1

    asyncio.run(run())


def test_ten_vad_hard_stop_finishes_when_moonshine_never_emits_eou():
    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(
            FakeWs(),
            {
                "stt": {"streaming": {"provider": "moonshine"}},
                "voice": {"semantic_turn": True, "turn_vad_hard_stop_ms": 1000},
            },
        )
        session.stt_session = FakeSttSession()
        session.turn_vad_gate = FakeVadGate()
        session._turn_speech_started = True
        session._last_turn_speech_at = time.monotonic() - 2.0
        finalized = 0

        async def finalize():
            nonlocal finalized
            finalized += 1

        session._finalize_utterance = finalize
        await session._feed_stt(_pcm16_chunk())

        assert finalized == 1

    asyncio.run(run())


@pytest.mark.parametrize("text", ["", "um", "hmm", "cough", "breathing noise"])
def test_barge_in_rejects_noise_and_fillers(text):
    assert web_server._duplex_meaningful_barge_text(text) is False


@pytest.mark.parametrize("text", ["stop", "wait", "actually", "yeah", "okay", "sure", "I need", "change that"])
def test_barge_in_accepts_control_words_and_real_phrases(text):
    assert web_server._duplex_meaningful_barge_text(text) is True


def test_barge_in_rejects_assistant_echo():
    assert web_server._duplex_meaningful_barge_text(
        "the weather is sunny", "Today the weather is sunny and warm."
    ) is False


def test_playback_done_is_the_server_listening_boundary():
    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(FakeWs(), {})
        session.state = "speaking"
        session._playback_pending.set()
        session._assistant_audio_started.set()

        await session.on_playback_done()

        assert session.state == "listening"
        assert session._playback_pending.is_set() is False
        assert session._assistant_audio_started.is_set() is False

    asyncio.run(run())


def test_barge_in_does_not_wait_for_cancelled_speaking_task():
    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(FakeWs(), {"stt": {"streaming": {"provider": "parakeet"}}})
        session.stt_session = FakeSttSession()
        release = asyncio.Event()
        speaking_task = asyncio.create_task(release.wait())
        session._speaking_task = speaking_task

        await asyncio.wait_for(session._trigger_barge_in(_pcm16_chunk()), timeout=0.1)

        assert session.state == "listening"
        assert speaking_task.done() is False
        release.set()
        await speaking_task

    asyncio.run(run())


def test_moonshine_barge_in_waits_for_commit_silence():
    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(
            FakeWs(),
            {
                "stt": {"streaming": {"provider": "moonshine"}},
                "voice": {"semantic_turn": True, "smart_turn_commit_delay_ms": 250},
            },
        )
        session.stt_session = FakeSttSession()
        finalized = 0

        async def finalize():
            nonlocal finalized
            finalized += 1

        session._finalize_utterance = finalize
        await session._trigger_barge_in(
            [_pcm16_chunk()], stt_seeded=True, eou=True, partial="wait"
        )
        assert finalized == 0

        session._smart_turn_accepted_at = time.monotonic() - 1.0
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# utterance -> instant_delta -> tts cycle
# ---------------------------------------------------------------------------


def test_utterance_instant_delta_tts_cycle(duplex_client, full_fakes):
    stt = full_fakes["stt"]
    stt.queue_response("", True)  # single chunk finalizes the utterance
    stt.final_text = "what time is it"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        assert conn.receive_json()["type"] == "ready"

        conn.send_json(_audio_msg(_pcm16_chunk()))

        utterance = _recv_until(conn, "utterance")
        assert utterance["text"] == "what time is it"
        assert utterance["speaker"] == "unknown"
        # Renderer audio is PCM16; the streaming STT contract is little-endian
        # Float32. Keep speaker-ID audio in PCM16, but convert the STT copy.
        assert struct.unpack_from("<f", stt.accepted_chunks[0])[0] == pytest.approx(100 / 32768)

        frames = _drain_until(conn, {"tts_end"})
        speaker = next(frame for frame in frames if frame["type"] == "speaker_update")
        assert speaker["utterance_id"] == utterance["utterance_id"]
        assert speaker["speaker"] == "owner"
        assert speaker["speaker_name"] == "Shereef"
        if not any(frame["type"] == "instant_done" for frame in frames):
            frames.append(_recv_until(conn, "instant_done"))
        deltas = [frame["text"] for frame in frames if frame["type"] == "instant_delta"]
        assert deltas == ["Hi", " there."]
        done = next(frame for frame in frames if frame["type"] == "instant_done")
        assert done["text"] == "Hi there."
        chunk = next(frame for frame in frames if frame["type"] == "tts_chunk")
        assert chunk["seq"] == 1
        assert chunk["data"] == "AAA="
        tts_start = next(frame for frame in frames if frame["type"] == "tts_start")
        assert tts_start["sample_rate"] == 24000

    # STT was re-armed for the next utterance immediately after finishing.
    assert stt.begin_count >= 2


def test_show_card_is_delivered_over_the_shared_duplex_path(duplex_client, full_fakes):
    full_fakes["instant"]["activity"] = {
        "kind": "card",
        "label": "Showing a card",
        "tool": "show_card",
        "card": {
            "title": "Weather",
            "body": "Sunny",
            "kind": "weather",
            "value": "25°C",
            "duration_ms": 5000,
        },
    }
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "show me the weather"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        card = _recv_until(conn, "card_show")

    assert card["card"] | {"id": "ignored"} == {
        "id": "ignored",
        "kind": "weather",
        "title": "Weather",
        "body": "Sunny",
        "value": "25°C",
        "duration": 5000,
    }


def test_voice_model_can_end_conversation_after_spoken_goodbye(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["[END_", "VOICE] Talk soon."]
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "end voice mode"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")
        _finish_playback(conn, timeout=10.0)

        assert _recv_until(conn, "conversation_end", timeout=10.0) == {"type": "conversation_end"}


def test_tts_start_reports_actual_backend_sample_rate(duplex_client, full_fakes, monkeypatch):
    """tts_start must carry whatever sample rate the TTS backend actually
    reports -- the duplex client must not have to assume a fixed 24 kHz."""
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "what time is it"

    def fake_stream(text):
        return [
            {"type": "start", "sample_rate": 16000, "provider": "fake-16k"},
            {"type": "chunk", "audio": "AAA="},
            {"type": "end", "provider": "fake-16k"},
        ]

    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", fake_stream)

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

        tts_start = _recv_until(conn, "tts_start")
        assert tts_start["sample_rate"] == 16000


def test_tts_start_falls_back_to_default_sample_rate_when_backend_omits_it(duplex_client, full_fakes, monkeypatch):
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "what time is it"

    def fake_stream(text):
        return [{"type": "chunk", "audio": "AAA="}]  # no start/sample_rate event at all

    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", fake_stream)

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

        tts_start = _recv_until(conn, "tts_start")
        assert tts_start["sample_rate"] == web_server._DUPLEX_DEFAULT_TTS_SAMPLE_RATE


def test_utterance_event_carries_guest_label(duplex_client, full_fakes):
    full_fakes["identify"]["label"] = "guest"
    full_fakes["identify"]["score"] = 0.6
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        utterance = _recv_until(conn, "utterance")
        assert utterance["speaker"] == "unknown"
        assert _recv_until(conn, "speaker_update")["speaker"] == "guest"


def test_partial_events_stream_before_eou(duplex_client, full_fakes):
    stt = full_fakes["stt"]
    stt.queue_response("what", False)
    stt.queue_response("what time", False)
    stt.queue_response("", True)
    stt.final_text = "what time is it"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        conn.send_json(_audio_msg(_pcm16_chunk()))
        conn.send_json(_audio_msg(_pcm16_chunk()))

        p1 = _recv_until(conn, "partial")
        assert p1["text"] == "what"
        p2 = _recv_until(conn, "partial")
        assert p2["text"] == "what time"
        utterance = _recv_until(conn, "utterance")
        assert utterance["text"] == "what time is it"


# ---------------------------------------------------------------------------
# Voice focus mode (speaker ID repurpose: attention, not access control --
# see docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md
# §4). "focus_mode" active is faked via the `focus_mode` fixture (defaults
# to inactive, matching a bare/un-enrolled install); "owner"/"guest"/
# "unknown" speaker labels come from the `identify_speaker` fixture.
# ---------------------------------------------------------------------------


class _RecordingWs:
    """Minimal fake WebSocket that just records every sent JSON payload --
    used by the direct ``_DuplexSession`` unit tests below, which need to
    inspect internal state (transcript, ``_run_turn`` calls) that the
    WS-protocol ``duplex_client`` fixture can't observe from the wire."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _make_focus_test_session(cfg=None):
    from tools.voice_instant_lane import RollingTranscript

    ws = _RecordingWs()
    session = web_server._DuplexSession(ws, cfg or {})
    session.transcript = RollingTranscript()
    session.stt_session = FakeSttSession()
    return session, ws


async def _await_speaker_id(session):
    tasks = tuple(session._speaker_tasks)
    if tasks:
        await asyncio.gather(*tasks)


def _speaker_update(ws):
    return next(f for f in ws.sent if f["type"] == "speaker_update")


def _patch_zoned(monkeypatch, **overrides):
    state = {
        "zone": "OWNER", "label": "owner", "score": 0.9, "audio_ms": 3000,
        "name": "Shereef", "model_mismatch": False, "embedding": [0.9, 0.1],
    }
    state.update(overrides)
    monkeypatch.setattr(web_server, "_duplex_identify_speaker_zoned", lambda pcm, cfg: dict(state))
    monkeypatch.setattr(web_server, "_duplex_owner_threshold", lambda cfg: 0.45)
    monkeypatch.setattr(web_server, "_duplex_competing_window_seconds", lambda cfg: 90.0)
    monkeypatch.setattr(web_server, "_duplex_continuity_seconds", lambda cfg: 120.0)
    return state


def test_focus_owner_utterance_passes_through_normally(monkeypatch):
    """Owner-matching utterances behave exactly as today, even with focus
    mode active. INVERSION NOTE: this test previously drove the retired
    single-threshold ``_duplex_identify_speaker`` seam; it now drives the
    zoned seam directly (zone OWNER, label owner)."""
    _patch_zoned(monkeypatch)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    monkeypatch.setattr(web_server, "_duplex_append_adaptive_embedding", lambda embedding: None)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "what time is it"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert utterance["speaker"] == "unknown"
        assert _speaker_update(ws)["speaker"] == "owner"
        assert "ignored" not in utterance
        assert session.transcript.turns == [{"role": "user", "content": "what time is it"}]
        assert run_calls == [("what time is it", "unknown")]
        assert session.state == "speaking"
        assert session._last_owner_activity_at is not None

    asyncio.run(run())


def test_parakeet_ignores_eou_until_speech_is_detected():
    async def run():
        class FakeWs:
            async def send_json(self, _payload):
                return None

        session = web_server._DuplexSession(
            FakeWs(),
            {"stt": {"streaming": {"provider": "parakeet"}}},
        )
        session.stt_session = FakeSttSession()
        gate = FakeVadGate()
        session.turn_vad_gate = gate
        finalized = 0

        async def finalize():
            nonlocal finalized
            finalized += 1

        session._finalize_utterance = finalize
        session.stt_session.queue_response("noise", True)
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 0

        gate.speaking = True
        session.stt_session.queue_response("hello", True)
        await session._feed_stt(_pcm16_chunk())
        assert finalized == 1

    asyncio.run(run())


def test_speaker_id_does_not_delay_the_instant_turn(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_identify(_pcm):
        entered.set()
        release.wait(timeout=5.0)
        return "owner", 0.9, "Shereef"

    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: False)
    monkeypatch.setattr(web_server, "_duplex_identify_speaker", slow_identify)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "turn on the light"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await session._speaking_task

        assert run_calls == [("turn on the light", "unknown")]
        assert next(f for f in ws.sent if f["type"] == "utterance")["speaker"] == "unknown"
        assert await asyncio.to_thread(entered.wait, 1.0)
        assert tuple(session._speaker_tasks)

        release.set()
        await _await_speaker_id(session)
        assert _speaker_update(ws)["speaker"] == "owner"

    asyncio.run(run())


def test_focus_enrolled_guest_passes_through_now(monkeypatch):
    """INVERSION (fail-open redesign, spec §4): an enrolled-non-owner match
    (score >= threshold) is now treated like an OWNER-zone acceptance for
    focus purposes -- never dropped -- while keeping its own "guest" badge.
    This REPLACES the old ``test_focus_guest_utterance_ignored_when_focus_active``,
    which asserted the opposite (every non-owner was ignored when focus was
    active) under the old fail-closed single-threshold design."""
    _patch_zoned(monkeypatch, zone="OWNER", label="guest", score=0.7, name="Bob")
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "turn off the lights"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert utterance["speaker"] == "unknown"
        assert _speaker_update(ws)["speaker"] == "guest"
        assert "ignored" not in utterance
        assert run_calls == [("turn off the lights", "unknown")]
        assert session.transcript.turns == [{"role": "user", "content": "turn off the lights"}]
        # A guest acceptance is not an owner activity -- continuity must not
        # be refreshed by it.
        assert session._last_owner_activity_at is None

    asyncio.run(run())


def test_focus_confident_other_is_attributed_async_without_blocking(monkeypatch):
    """Speaker focus attributes a different voice but never blocks its turn."""
    _patch_zoned(monkeypatch, zone="CONFIDENT_OTHER", label="unknown", score=0.1, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "is anybody there"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert utterance["speaker"] == "unknown"
        assert _speaker_update(ws)["speaker"] == "unknown"
        assert "ignored" not in utterance
        assert run_calls == [("is anybody there", "unknown")]
        assert session.transcript.turns == [{"role": "user", "content": "is anybody there"}]
        assert session._other_voice_active() is True

    asyncio.run(run())


def test_focus_abstain_alone_in_room_never_dropped(monkeypatch):
    """The fail-open invariant (spec §4): an ABSTAIN (short/degraded audio,
    or a mid-range score) is never dropped when nothing confidently
    different has been heard recently -- even on the very first utterance
    of a session, with no continuity established yet."""
    _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "hello there"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert "ignored" not in utterance
        assert run_calls == [("hello there", "unknown")]

    asyncio.run(run())


def test_focus_abstain_resolves_to_owner_via_continuity(monkeypatch):
    zoned_state = _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session._last_owner_activity_at = time.monotonic() - 10.0  # owner spoke 10s ago
        session.stt_session.final_text = "and also this"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert "ignored" not in utterance
        assert utterance["speaker"] == "unknown"
        assert _speaker_update(ws)["speaker"] == "owner"
        assert run_calls == [("and also this", "unknown")]

    asyncio.run(run())


def test_short_audio_cannot_establish_owner_identity(monkeypatch):
    _patch_zoned(
        monkeypatch, zone="OWNER", label="owner", score=0.99,
        audio_ms=500, name="Shereef",
    )
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        await session._identify_utterance_speaker(b"\0\0" * 8000, "short")

        update = _speaker_update(ws)
        assert update["speaker"] == "unknown"
        assert update["speaker_name"] is None
        assert session._last_owner_activity_at is None

    asyncio.run(run())


def test_focus_continuity_expires_after_window(monkeypatch):
    _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session._last_owner_activity_at = time.monotonic() - 130.0  # older than 120s continuity window
        session.stt_session.final_text = "still here"

        async def fake_run_turn(text, speaker_label, cancel_event):
            pass

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        # Still not dropped (flag unarmed) -- just not relabeled as owner.
        assert "ignored" not in utterance
        assert utterance["speaker"] == "unknown"

    asyncio.run(run())


def test_focus_abstain_is_not_dropped_while_competing_flag_armed(monkeypatch):
    _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session._other_voice_active_until = time.monotonic() + 60.0  # armed
        session.stt_session.final_text = "hmm"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert "ignored" not in utterance
        assert run_calls == [("hmm", "unknown")]

    asyncio.run(run())


def test_focus_competing_flag_decays_after_window(monkeypatch):
    """The competing-voice flag naturally decays once
    competing_window_seconds elapses -- nothing needs to actively disarm it."""
    _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, name=None)
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)

    async def run():
        session, ws = _make_focus_test_session()
        session._other_voice_active_until = time.monotonic() - 1.0  # armed, but already expired
        session.stt_session.final_text = "hmm"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert "ignored" not in utterance
        assert run_calls == [("hmm", "unknown")]

    asyncio.run(run())


def test_focus_direct_owner_with_enough_audio_appends_adaptive_embedding(monkeypatch):
    _patch_zoned(monkeypatch, zone="OWNER", label="owner", score=0.9, audio_ms=3500, embedding=[0.5, 0.5])
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    appended = []
    monkeypatch.setattr(web_server, "_duplex_append_adaptive_embedding", lambda embedding: appended.append(embedding))

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "hey there"

        async def fake_run_turn(text, speaker_label, cancel_event):
            pass

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        assert appended == [[0.5, 0.5]]

    asyncio.run(run())


def test_focus_direct_owner_below_adaptive_duration_floor_does_not_append(monkeypatch):
    _patch_zoned(monkeypatch, zone="OWNER", label="owner", score=0.9, audio_ms=1000, embedding=[0.5, 0.5])
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    appended = []
    monkeypatch.setattr(web_server, "_duplex_append_adaptive_embedding", lambda embedding: appended.append(embedding))

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "hey"

        async def fake_run_turn(text, speaker_label, cancel_event):
            pass

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        assert appended == []

    asyncio.run(run())


def test_focus_continuity_resolved_owner_does_not_append_adaptive(monkeypatch):
    """Only a DIRECT-score OWNER-zone acceptance feeds the adaptive ring --
    a continuity-resolved ABSTAIN never does, even though its badge also
    reads "owner" (spec Part 1.4: "not via continuity")."""
    _patch_zoned(monkeypatch, zone="ABSTAIN", label="unknown", score=0.35, audio_ms=4000, embedding=[0.5, 0.5])
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    appended = []
    monkeypatch.setattr(web_server, "_duplex_append_adaptive_embedding", lambda embedding: appended.append(embedding))

    async def run():
        session, ws = _make_focus_test_session()
        session._last_owner_activity_at = time.monotonic() - 5.0
        session.stt_session.final_text = "still talking"

        async def fake_run_turn(text, speaker_label, cancel_event):
            pass

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        assert appended == []

    asyncio.run(run())


def test_focus_no_enrollment_passes_guest_through(monkeypatch):
    """No owner enrolled (or model unavailable) -- focus_mode_active is
    False regardless of the "owner" setting default, so a non-owner speaker
    must never be filtered on a bare install."""
    monkeypatch.setattr(web_server, "_duplex_identify_speaker", lambda pcm: ("guest", 0.55, "Bob"))
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: False)

    async def run():
        session, ws = _make_focus_test_session()
        session.stt_session.final_text = "turn off the lights"
        run_calls = []

        async def fake_run_turn(text, speaker_label, cancel_event):
            run_calls.append((text, speaker_label))

        session._run_turn = fake_run_turn
        await session._finalize_utterance()
        await _await_speaker_id(session)
        if session._speaking_task is not None:
            await session._speaking_task

        utterance = next(f for f in ws.sent if f["type"] == "utterance")
        assert "ignored" not in utterance
        assert _speaker_update(ws)["speaker"] == "guest"
        assert run_calls == [("turn off the lights", "unknown")]
        assert session.transcript.turns == [{"role": "user", "content": "turn off the lights"}]

    asyncio.run(run())


def test_focus_off_setting_passes_guest_through(duplex_client, full_fakes):
    """``voice.speaker_id.focus_mode: off`` (simulated here via the
    focus_mode fixture, since focus_mode_active already folds the setting
    into one flag) is full passthrough end-to-end over the WS protocol."""
    full_fakes["focus"]["active"] = False
    full_fakes["identify"]["label"] = "guest"
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        utterance = _recv_until(conn, "utterance")
        assert utterance["speaker"] == "unknown"
        assert _recv_until(conn, "speaker_update")["speaker"] == "guest"
        assert "ignored" not in utterance

        # The turn still runs normally (instant lane -> TTS), unlike the
        # ignored case.
        _recv_until(conn, "tts_end")


def test_focus_confident_other_is_attributed_and_still_gets_tts(duplex_client, full_fakes):
    """A different speaker is attributed asynchronously and remains interactive."""
    full_fakes["focus"]["active"] = True
    full_fakes["zoned"].update(
        {"zone": "CONFIDENT_OTHER", "label": "unknown", "score": 0.1, "name": None}
    )
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        utterance = _recv_until(conn, "utterance")
        assert utterance["speaker"] == "unknown"
        assert "ignored" not in utterance
        assert _recv_until(conn, "speaker_update")["speaker"] == "unknown"
        _recv_until(conn, "tts_end")


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


def test_deep_worker_mode_gets_execution_tools_and_verification_prompt(monkeypatch):
    import run_agent
    from hermes_cli import web_server

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tool_start_callback = kwargs.get("tool_start_callback")
            self.tool_complete_callback = kwargs.get("tool_complete_callback")

        def run_conversation(self, task_text, conversation_history=None):
            self.tool_start_callback("c1", "terminal", {"cmd": "test"})
            self.tool_complete_callback("c1", "terminal", {}, "passed")
            return {"final_response": "I finished the work and verification passed."}

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(
        web_server,
        "load_config",
        lambda: {
            "model": {
                # Some existing profiles store this under `model` rather than
                # `default`; delegation must not send an empty model slug.
                "model": "deepseek-v4-flash",
                "provider": "opencode-go",
                "base_url": "https://opencode.ai/zen/go/v1",
                "api_mode": "chat_completions",
            }
        },
    )
    activity = []

    result = web_server._duplex_run_deep_task(
        [{"role": "user", "content": "context"}],
        "do the work",
        mode="delegating",
        activity_callback=activity.append,
    )

    assert result == "I finished the work and verification passed."
    assert "terminal" in captured["enabled_toolsets"]
    assert "verify" in captured["ephemeral_system_prompt"].lower()
    assert captured["platform"] == "voice-subagent"
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["provider"] == "opencode-go"
    assert [event["status"] for event in activity] == ["started", "completed"]


def test_escalation_event_order(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["[ESCALATE] On it, one sec."]
    full_fakes["deep"]["text"] = "Here's what I found."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "plan my whole week"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")

        instant_done = _recv_until(conn, "instant_done")
        assert instant_done["text"] == ""

        escalated = _recv_until(conn, "escalated")
        assert escalated["ack_text"] == "On it, one sec."
        assert escalated["mode"] == "thinking"
        task_id = escalated["task_id"]
        assert task_id

        # Ack TTS cycle.
        assert _recv_until(conn, "tts_start") == {"type": "tts_start", "sample_rate": 24000}
        _finish_playback(conn)

        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["task_id"] == task_id
        assert deep_result["text"] == "Here's what I found."

        # Deep-result TTS cycle.
        assert _recv_until(conn, "tts_start") == {"type": "tts_start", "sample_rate": 24000}
        _finish_playback(conn)


def test_delegation_routes_work_to_background_subagent(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["[DELEGATE] I'll hand this to a sub-agent."]
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "fix the project and verify it"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()
        conn.send_json(_audio_msg(_pcm16_chunk()))
        escalated = _recv_until(conn, "escalated")
        assert escalated["mode"] == "delegating"
        assert "sub-agent" in escalated["ack_text"]
        _finish_playback(conn)
        _recv_until(conn, "deep_result", timeout=10.0)

    assert full_fakes["deep"]["modes"] == ["delegating"]


def test_instant_tool_activity_emits_ui_event_and_spoken_cue(duplex_client, full_fakes):
    full_fakes["instant"]["activity"] = {"kind": "web", "label": "Searching the web", "tool": "web_search"}
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "search the weather"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()
        conn.send_json(_audio_msg(_pcm16_chunk()))
        activity = _recv_until(conn, "activity")
        assert activity["kind"] == "web"
        assert activity["label"] == "Searching the web"
        _recv_until(conn, "tts_end")

    assert "Let me search for that." in full_fakes["tts"]


def test_long_background_work_keeps_talking_until_result(duplex_client, full_fakes, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_DUPLEX_DEEP_CUE_INTERVAL_SECONDS", 0.01)
    full_fakes["instant"]["deltas"] = ["[DELEGATE] I'll hand this to a sub-agent."]
    full_fakes["deep"]["delay"] = 1.2
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "do the work"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "escalated")
        _finish_playback(conn)
        activity = _recv_with_playback_acks(conn, "activity", timeout=5.0)
        assert activity["label"] == "Sub-agent is still working"
        result = _recv_with_playback_acks(conn, "deep_result", timeout=10.0)
        assert result["text"] == "Deep answer."

    assert any("still working" in text for text in full_fakes["tts"])

def test_escalation_deep_task_failure_speaks_apology_and_errors(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["[ESCALATE] Sure, one moment."]
    full_fakes["deep"]["raises"] = RuntimeError("agent blew up")
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "do something complicated"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "escalated")
        assert _recv_until(conn, "tts_start") == {"type": "tts_start", "sample_rate": 24000}
        _finish_playback(conn)

        error = _recv_until(conn, "error", timeout=10.0)
        assert "agent blew up" in error["error"]

        deep_result = _recv_until(conn, "deep_result", timeout=5.0)
        assert deep_result["text"]  # spoken apology, non-empty

        assert conn.receive_json() == {"type": "tts_start", "sample_rate": 24000}
        _finish_playback(conn)


def test_non_owner_can_now_escalate(duplex_client, full_fakes):
    """Speaker ID is voice FOCUS, not access control (spec §4 repurpose):
    the old owner-only escalation gate is gone, so a guest's (or unknown
    speaker's) complex ask escalates to the deep agent exactly like the
    owner's would."""
    full_fakes["identify"]["label"] = "guest"
    full_fakes["instant"]["deltas"] = ["[ESCALATE] On it."]
    full_fakes["deep"]["text"] = "Here's what I found."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "do something complicated"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        utterance = _recv_until(conn, "utterance")
        assert utterance["speaker"] == "unknown"
        assert _recv_until(conn, "speaker_update")["speaker"] == "guest"

        escalated = _recv_until(conn, "escalated", timeout=10.0)
        assert escalated["ack_text"] == "On it."

        _finish_playback(conn)
        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["text"] == "Here's what I found."

    assert full_fakes["deep"]["calls"] == ["do something complicated"]


def test_escalation_disabled_never_escalates(duplex_client, full_fakes, monkeypatch):
    from tools import voice_instant_lane as vil

    monkeypatch.setattr(vil, "escalation_enabled", lambda cfg=None: False)
    full_fakes["instant"]["deltas"] = ["[ESCALATE] On it."]
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "plan my week"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

        seen_types = []
        deadline = time.monotonic() + 3.0
        frame = conn.receive_json()
        while time.monotonic() < deadline:
            seen_types.append(frame["type"])
            if frame["type"] == "tts_end":
                break
            frame = conn.receive_json()
        assert "escalated" not in seen_types


# ---------------------------------------------------------------------------
# Barge-in
# ---------------------------------------------------------------------------


def test_barge_in_cancels_tts_and_instant_stream(duplex_client, full_fakes):
    # A long-ish, artificially paced instant reply so the session stays in
    # the "speaking" state long enough for the barge-in audio below to land
    # (the fakes would otherwise finish the whole turn in well under a
    # millisecond, closing the barge-in window before any chunk arrives).
    full_fakes["instant"]["deltas"] = [
        "First sentence. ", "This ", "is ", "a ", "long ", "reply ", "that ", "keeps ", "going."
    ]
    full_fakes["instant"]["delay"] = 0.05
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "tell me a long story"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")
        _recv_until(conn, "tts_start", timeout=10.0)

        # Flip the VAD gate to "speaking" and feed enough chunks to cross
        # the sustained-speech threshold (_DUPLEX_BARGE_IN_STREAK_MS).
        full_fakes["vad"].speaking = True
        stt.queue_response("wait stop", False)
        for _ in range(30):
            conn.send_json(_audio_msg(_pcm16_chunk()))

        barge_in = _recv_until(conn, "barge_in", timeout=10.0)
        assert barge_in == {"type": "barge_in"}

        # After barge-in, the session accepts a fresh utterance without
        # erroring (proves it returned to the listening state).
        stt.queue_response("", True)
        stt.final_text = "second utterance"
        full_fakes["vad"].speaking = False
        conn.send_json(_audio_msg(_pcm16_chunk()))
        utterance = _recv_until(conn, "utterance", timeout=10.0)
        assert utterance["text"] == "second utterance"


def test_speech_before_assistant_playback_does_not_false_barge_in(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["This ", "reply ", "starts ", "after ", "a delay."]
    full_fakes["instant"]["delay"] = 0.05
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "are you doing great"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

        full_fakes["vad"].speaking = True
        for _ in range(30):
            conn.send_json(_audio_msg(_pcm16_chunk()))

        frames = _drain_until(conn, {"tts_start"}, timeout=10.0)
        assert not any(frame["type"] == "barge_in" for frame in frames)


# ---------------------------------------------------------------------------
# Speaker-confirmed barge-in (spec §3: echo/other-voice-proof). Direct
# ``_DuplexSession`` unit tests (like the Smart Turn tests above) rather
# than the WS-protocol client -- these need to feed dozens of tightly
# controlled chunks and inspect internal state precisely.
# ---------------------------------------------------------------------------


def _make_barge_test_session(cfg=None):
    ws = _RecordingWs()
    session = web_server._DuplexSession(ws, cfg or {})
    session.stt_session = FakeSttSession()
    vad = FakeVadGate()
    vad.speaking = True
    session.vad_gate = vad
    session._assistant_audio_started.set()
    session.state = "speaking"
    return session, ws, vad


def _patch_barge_negative(monkeypatch, *, tts_score=-1.0, other_voice_active=False, zoned_zone="ABSTAIN"):
    monkeypatch.setattr(web_server, "_duplex_owner_threshold", lambda cfg: 0.45)
    monkeypatch.setattr(web_server, "_duplex_competing_window_seconds", lambda cfg: 90.0)
    monkeypatch.setattr(web_server, "_duplex_continuity_seconds", lambda cfg: 120.0)
    monkeypatch.setattr(web_server, "_duplex_tts_echo_score", lambda pcm, cfg: tts_score)
    monkeypatch.setattr(
        web_server, "_duplex_identify_speaker_zoned",
        lambda pcm, cfg: {
            "zone": zoned_zone, "label": "unknown", "score": 0.1, "audio_ms": 3000,
            "name": None, "model_mismatch": False, "embedding": None,
        },
    )


def test_barge_in_uncertain_candidate_barges_immediately(monkeypatch):
    """INVERSION (fail-open redesign, spec §3): the old positive-owner gate
    (``_barge_owner_gate_blocks``, required an owner match before ANY
    barge) is replaced by confirm-negative -- an uncertain candidate (no TTS
    echo, no armed competing flag) barges as soon as the plain VAD+text
    streak/eou gate is satisfied, WITHOUT waiting for a confirm window.
    This replaces ``test_barge_in_owner_match_confirms_and_barges``, which
    asserted the opposite (barge only after a positive owner match)."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    _patch_barge_negative(monkeypatch)

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        # ~400ms: past the plain streak_ms threshold (320ms) but well short
        # of the old ~0.7s confirm window -- proves the redesign doesn't
        # wait for a full confirm window before barging.
        for _ in range(20):
            if ws.sent:
                break
            await session._feed_barge_in(chunk)

        assert ws.sent.count({"type": "barge_in"}) == 1
        assert session.state == "listening"

    asyncio.run(run())


def test_barge_in_tts_echo_suppressed(monkeypatch):
    """A candidate confidently matching Marvi's own TTS voice (self-echo,
    spec Part 1.3) never barges in, however long it sustains. Replaces
    ``test_barge_in_non_owner_match_suppressed`` -- suppression is now keyed
    on a NEGATIVE match (echo/competing-other), not "failed to positively
    match the owner"."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    _patch_barge_negative(monkeypatch, tts_score=0.95)

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        for _ in range(300):
            await session._feed_barge_in(chunk)

        assert ws.sent == []
        assert session.state == "speaking"  # never interrupted

    asyncio.run(run())


def test_barge_in_owner_speech_wins_over_tts_echo(monkeypatch):
    """Mixed owner speech must not be discarded merely because playback is audible."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    _patch_barge_negative(monkeypatch, tts_score=0.95, zoned_zone="OWNER")

    async def run():
        session, _ws, _vad = _make_barge_test_session()
        session._barge_candidate_audio = [_pcm16_chunk()] * 20
        session._barge_candidate_bytes_seen = sum(
            len(chunk) for chunk in session._barge_candidate_audio
        )
        assert await session._barge_negative_match_blocks() is False

    asyncio.run(run())


def test_barge_in_confident_other_suppressed_only_while_armed(monkeypatch):
    """A CONFIDENT_OTHER match only suppresses barge-in while the
    competing-voice flag is armed -- an uncertain/ABSTAIN candidate is not
    enough on its own (that's the default fail-open case, see
    ``test_barge_in_uncertain_candidate_barges_immediately``)."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    _patch_barge_negative(monkeypatch, zoned_zone="CONFIDENT_OTHER")

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session._other_voice_active_until = time.monotonic() + 60.0  # armed
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        for _ in range(300):
            await session._feed_barge_in(chunk)

        assert ws.sent == []
        assert session.state == "speaking"

    asyncio.run(run())


def test_barge_in_recheck_on_fresh_window_then_barges_once_no_longer_negative(monkeypatch):
    """A candidate initially screened as TTS echo, then genuinely changes
    (the score drops on a later, fresher window) -- re-checked at most every
    ~300ms of new audio, and barges once it's no longer negative, rather
    than being stuck suppressed forever from one early check."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: True)
    monkeypatch.setattr(web_server, "_duplex_owner_threshold", lambda cfg: 0.45)
    monkeypatch.setattr(web_server, "_duplex_competing_window_seconds", lambda cfg: 90.0)
    monkeypatch.setattr(web_server, "_duplex_continuity_seconds", lambda cfg: 120.0)
    monkeypatch.setattr(
        web_server, "_duplex_identify_speaker_zoned",
        lambda pcm, cfg: {
            "zone": "ABSTAIN", "label": "unknown", "score": 0.1, "audio_ms": 3000,
            "name": None, "model_mismatch": False, "embedding": None,
        },
    )
    scores = iter([0.95, 0.95, 0.1])  # echo, echo, then genuinely not echo
    score_calls = []

    def fake_tts_score(pcm, cfg):
        value = next(scores, 0.1)
        score_calls.append(value)
        return value

    monkeypatch.setattr(web_server, "_duplex_tts_echo_score", fake_tts_score)

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        for _ in range(400):
            if ws.sent:
                break
            await session._feed_barge_in(chunk)

        assert ws.sent.count({"type": "barge_in"}) == 1
        assert score_calls == [0.95, 0.95, 0.1]

    asyncio.run(run())


def test_barge_in_model_unavailable_falls_back_to_vad_only(monkeypatch):
    """Speaker model unavailable -- folded into focus_mode_active() being
    False -- must behave exactly like plain VAD-only barge-in: fire as soon
    as the streak/text gate is satisfied, without ever running the negative
    screen (TTS-echo or zoned-identify)."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: False)
    tts_calls = []
    zoned_calls = []
    monkeypatch.setattr(web_server, "_duplex_tts_echo_score", lambda pcm, cfg: tts_calls.append(1) or -1.0)
    monkeypatch.setattr(
        web_server, "_duplex_identify_speaker_zoned",
        lambda pcm, cfg: zoned_calls.append(1) or {
            "zone": "ABSTAIN", "label": "unknown", "score": 0.0, "audio_ms": 0,
            "name": None, "model_mismatch": False, "embedding": None,
        },
    )

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        # ~400ms: past the plain streak_ms threshold (320ms) but well short
        # of the ~0.7s confirm window -- proves confirmation isn't gating.
        for _ in range(20):
            await session._feed_barge_in(chunk)

        assert ws.sent.count({"type": "barge_in"}) == 1
        assert tts_calls == []
        assert zoned_calls == []

    asyncio.run(run())


def test_barge_in_focus_off_falls_back_to_vad_only(monkeypatch):
    """``voice.speaker_id.focus_mode: off`` (folded into focus_mode_active()
    being False the same way "no owner enrolled" is) -- same VAD-only
    passthrough as before this feature existed."""
    monkeypatch.setattr(web_server, "_duplex_focus_mode_active", lambda cfg: False)
    tts_calls = []
    monkeypatch.setattr(web_server, "_duplex_tts_echo_score", lambda pcm, cfg: tts_calls.append(1) or -1.0)

    async def run():
        session, ws, _vad = _make_barge_test_session()
        session.stt_session.queue_response("wait stop", False)
        chunk = _pcm16_chunk()

        for _ in range(20):
            await session._feed_barge_in(chunk)

        assert ws.sent.count({"type": "barge_in"}) == 1
        assert tts_calls == []

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Instant lane unreachable -> fallback
# ---------------------------------------------------------------------------


def test_instant_lane_down_falls_back_to_deep_task_with_one_error(duplex_client, full_fakes):
    full_fakes["instant"]["raises"] = RuntimeError("no provider configured")
    full_fakes["deep"]["text"] = "Fallback answer."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello marvi"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")
        error = _recv_until(conn, "error", timeout=10.0)
        assert "instant" in error["error"].lower() or "no provider" in error["error"].lower()

        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["text"] == "Fallback answer."

        # Exactly one error event for this turn.
        frame = conn.receive_json()
        seen_error_again = False
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if frame["type"] == "error":
                seen_error_again = True
            if frame["type"] == "tts_end":
                break
            frame = conn.receive_json()
        assert seen_error_again is False


def test_empty_instant_reply_falls_back_instead_of_leaving_listening_ui(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = []
    full_fakes["deep"]["text"] = "Fallback answer."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello marvi"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")
        error = _recv_until(conn, "error", timeout=10.0)
        assert "empty response" in error["error"]
        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["text"] == "Fallback answer."


def test_instant_lane_down_and_non_owner_still_falls_back_to_deep_task(duplex_client, full_fakes):
    """Non-owner + instant lane down: the fallback-to-deep-task path (spec
    "Error handling") isn't owner-gated any more than escalation is -- a
    guest's utterance still reaches the deep agent when the instant lane is
    unreachable."""
    full_fakes["identify"]["label"] = "guest"
    full_fakes["instant"]["raises"] = RuntimeError("no provider configured")
    full_fakes["deep"]["text"] = "Fallback answer."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello marvi"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")
        error = _recv_until(conn, "error", timeout=10.0)
        assert error["type"] == "error"

        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["text"] == "Fallback answer."

    assert full_fakes["deep"]["calls"] == ["hello marvi"]


# ---------------------------------------------------------------------------
# Instant-lane warm-up at session open
# ---------------------------------------------------------------------------


def test_warmup_runs_on_connect_before_first_utterance(duplex_client, full_fakes):
    """The instant lane must warm up as soon as the WS connects -- not
    lazily on the first utterance."""
    with duplex_client.websocket_connect(_duplex_url()) as conn:
        assert conn.receive_json() == {"type": "ready"}

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not full_fakes["warm"]["calls"]:
        time.sleep(0.01)
    assert len(full_fakes["warm"]["calls"]) == 1


def test_warmup_does_not_block_the_ready_event(duplex_client, full_fakes, monkeypatch):
    """Warm-up runs on a background thread; a slow warm-up must not delay
    ``ready``."""
    def slow_warm(transcript, cfg):
        time.sleep(2.0)
        return {"ok": True, "construct_ms": 2000.0}

    monkeypatch.setattr(web_server, "_duplex_warm_instant_lane", slow_warm)

    start = time.monotonic()
    with duplex_client.websocket_connect(_duplex_url()) as conn:
        assert conn.receive_json() == {"type": "ready"}
    assert time.monotonic() - start < 1.0


def test_first_utterance_finds_pre_warmed_agent(duplex_client, full_fakes):
    """A turn arriving after warm-up completed must reuse the same warm
    state rather than re-triggering construction from scratch -- exercised
    here via the seam: warm-up ran once at connect, and the instant-reply
    fake reports the warm hit it was told to report."""
    full_fakes["instant"]["warm_status"] = {"hit": True, "construct_ms": None}
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "tts_end")

    assert full_fakes["instant"]["warm_calls"] == [{"hit": True, "construct_ms": None}]


def test_lazy_fallback_still_works_when_warmup_reports_a_miss(duplex_client, full_fakes):
    """If warm-up hasn't finished (or failed), the instant lane's own lazy
    construct-if-missing path is the documented fallback -- the turn still
    completes normally, just reporting a cache miss."""
    full_fakes["warm"]["result"] = {"ok": False, "error": "not ready yet", "construct_ms": 5.0}
    full_fakes["instant"]["warm_status"] = {"hit": False, "construct_ms": 812.0}
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        frames = _drain_until(conn, {"tts_end"})
        deltas = [f["text"] for f in frames if f["type"] == "instant_delta"]
        assert deltas == ["Hi", " there."]


# ---------------------------------------------------------------------------
# [VOICE-PERF] structured logging
# ---------------------------------------------------------------------------


def _read_voice_perf_lines(timeout: float = 5.0) -> list[str]:
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "logs" / "voice-perf.log"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return [line for line in text.splitlines() if line.strip()]
        time.sleep(0.02)
    return []


def test_session_open_perf_line_is_emitted(duplex_client, full_fakes):
    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready

    lines = _read_voice_perf_lines()
    session_lines = [l for l in lines if "session_open" in l]
    assert session_lines, f"no session_open [VOICE-PERF] line found in {lines!r}"
    line = session_lines[0]
    assert "[VOICE-PERF] session_open" in line
    assert "session_id=" in line
    assert "agent_warm_ok=true" in line
    assert "agent_warm_construct_ms=12" in line
    assert "provider=fake-provider" in line
    assert "model=fake-model" in line


def test_turn_perf_line_has_all_documented_fields(duplex_client, full_fakes):
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "what time is it"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "tts_end")

    lines = _read_voice_perf_lines()
    turn_lines = [l for l in lines if " turn " in l]
    assert turn_lines, f"no turn [VOICE-PERF] line found in {lines!r}"
    line = turn_lines[-1]
    assert line.startswith("[VOICE-PERF] turn") or "[VOICE-PERF] turn" in line
    for field in (
        "session_id=", "utterance_id=", "eou_to_instant_start_ms=",
        "instant_first_delta_ms=", "first_delta_to_first_tts_chunk_ms=",
        "total_first_audio_ms=", "agent_warm=", "agent_warm_construct_ms=",
        "deferred_context=", "deferred_context_load_ms=", "tts_max_gap_ms=",
        "escalated=",
    ):
        assert field in line, f"missing {field!r} in perf line: {line!r}"
    assert "escalated=false" in line


def test_turn_perf_line_marks_escalated_turns(duplex_client, full_fakes):
    full_fakes["instant"]["deltas"] = ["[ESCALATE] On it, one sec."]
    full_fakes["deep"]["text"] = "Here's what I found."
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "plan my whole week"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "escalated", timeout=10.0)
        _finish_playback(conn)
        _recv_until(conn, "deep_result", timeout=10.0)

    lines = _read_voice_perf_lines()
    turn_lines = [l for l in lines if " turn " in l]
    assert any("escalated=true" in l for l in turn_lines)


# ---------------------------------------------------------------------------
# TTS synth-ahead pipeline
# ---------------------------------------------------------------------------


def test_partial_reply_flushes_before_sentence_punctuation():
    text = "This response is long enough to start speaking before punctuation arrives"
    ready, rest = web_server._split_ready_sentences(text)
    assert ready
    assert len(ready[0]) >= web_server._DUPLEX_TTS_EARLY_SEGMENT_CHARS // 2
    assert rest


def test_first_tts_segment_flushes_one_completed_word():
    ready, rest = web_server._split_ready_sentences(
        "Hello there, Shereef.", first_word=True
    )
    assert ready == ["Hello"]
    assert rest.strip() == "there, Shereef."


def test_first_tts_chunk_is_emitted_before_first_segment_finishes(monkeypatch):
    release = threading.Event()

    def streaming_tts(_text):
        yield {"type": "start", "sample_rate": 24000}
        yield {"type": "chunk", "audio": "FIRST"}
        release.wait(timeout=5.0)
        yield {"type": "chunk", "audio": "SECOND"}

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", streaming_tts)

    class Session:
        def __init__(self):
            self._playback_pending = threading.Event()
            self._assistant_audio_started = threading.Event()
            self.events = []

        def _emit_sync(self, event):
            self.events.append(event)

    session = Session()
    pipeline = web_server._DuplexTtsPipeline(session, threading.Event())
    pipeline.submit("A complete first segment.")

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not any(
        event.get("data") == "FIRST" for event in session.events
    ):
        time.sleep(0.01)

    assert any(event.get("data") == "FIRST" for event in session.events)
    assert not any(event.get("data") == "SECOND" for event in session.events)
    release.set()
    assert pipeline.finish()[0] is True


@pytest.fixture
def ordered_tts_chunks(monkeypatch):
    """Unlike the generic ``tts_chunks`` fixture, echoes the synthesized
    sentence back in the chunk's ``audio`` field (base64) so a test can
    verify chunks were emitted in submission order even though synthesis
    itself may run concurrently across sentences."""
    calls: list[str] = []

    def fake_stream(text):
        calls.append(text)
        payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return [
            {"type": "start", "sample_rate": 24000},
            {"type": "chunk", "audio": payload},
            {"type": "end"},
        ]

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", fake_stream)
    return calls


def test_synth_ahead_preserves_sentence_order_on_the_wire(
    duplex_client, stt_session, identify_speaker, ordered_tts_chunks, vad_gate, deep_task, warm_lane, monkeypatch,
):
    """Sentences synthesize with bounded lookahead (possibly out of synthesis
    order across the two workers), but chunks must still land on the wire in
    submission order."""
    def fake_stream(transcript, utterance, *, allow_escalation, activity_callback=None, warm_status_callback=None):
        for d in ["First sentence. ", "Second sentence. ", "Third sentence."]:
            yield d

    monkeypatch.setattr(web_server, "_duplex_stream_instant_reply", fake_stream)

    stt_session.queue_response("", True)
    stt_session.final_text = "tell me three things"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

        frames = _drain_until(conn, {"tts_end"})

    chunks = [f for f in frames if f["type"] == "tts_chunk"]
    decoded = [base64.b64decode(f["data"]).decode("utf-8") for f in chunks]
    assert decoded == ["First", "sentence.", "Second sentence.", "Third sentence."]
    # seq is monotonically increasing across the whole turn, not reset
    # per-sentence -- proves all three shared one tts_start/tts_end cycle.
    assert [f["seq"] for f in chunks] == [1, 2, 3, 4]


def test_synth_ahead_reports_gap_between_sentences_in_perf_line(
    duplex_client, stt_session, identify_speaker, vad_gate, deep_task, warm_lane, monkeypatch,
):
    """A synthesis backend with a deliberate per-call delay produces a
    measurable tts_max_gap_ms in the turn's [VOICE-PERF] line."""
    def fake_tts_stream(text):
        time.sleep(0.05)
        return [{"type": "start", "sample_rate": 24000}, {"type": "chunk", "audio": "AAA="}, {"type": "end"}]

    def fake_instant_stream(transcript, utterance, *, allow_escalation, activity_callback=None, warm_status_callback=None):
        for d in ["First. ", "Second."]:
            yield d

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", fake_tts_stream)
    monkeypatch.setattr(web_server, "_duplex_stream_instant_reply", fake_instant_stream)

    stt_session.queue_response("", True)
    stt_session.final_text = "tell me two things"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "tts_end")

    lines = _read_voice_perf_lines()
    turn_lines = [l for l in lines if " turn " in l]
    assert turn_lines
    # Some non-trivial gap value was recorded (a plain int, not the "-"
    # placeholder used when unavailable).
    import re

    m = re.search(r"tts_max_gap_ms=(\S+)", turn_lines[-1])
    assert m is not None
    assert m.group(1) != "-"


# ---------------------------------------------------------------------------
# Barge-in cancels the TTS pipeline promptly
# ---------------------------------------------------------------------------


def test_barge_in_drops_queued_but_unemitted_sentences(
    duplex_client, stt_session, identify_speaker, vad_gate, deep_task, warm_lane, monkeypatch,
):
    """A sentence still queued/mid-synthesis in the pipeline when barge-in
    fires must never reach the wire."""
    release = threading.Event()

    def slow_tts_stream(text):
        # Let the first sentence start playback, then hold the second so the
        # test can interrupt while later synthesis is still queued.
        if "Second" in text:
            release.wait(timeout=5.0)
        return [{"type": "start", "sample_rate": 24000}, {"type": "chunk", "audio": "AAA="}, {"type": "end"}]

    def fake_instant_stream(transcript, utterance, *, allow_escalation, activity_callback=None, warm_status_callback=None):
        for d in ["First. ", "Second. ", "Third."]:
            yield d

    monkeypatch.setattr(web_server, "_duplex_stream_tts_chunks", slow_tts_stream)
    monkeypatch.setattr(web_server, "_duplex_stream_instant_reply", fake_instant_stream)

    stt_session.queue_response("", True)
    stt_session.final_text = "tell me three things"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "tts_start", timeout=10.0)

        # Trigger barge-in after first playback starts but while the remaining
        # sentences are blocked/queued and must never be emitted.
        vad_gate.speaking = True
        stt_session.queue_response("wait stop", False)
        for _ in range(30):
            conn.send_json(_audio_msg(_pcm16_chunk()))
        barge_in = _recv_until(conn, "barge_in", timeout=10.0)
        assert barge_in == {"type": "barge_in"}
        release.set()

        # Send a second, immediately-final utterance right after barge-in --
        # `conn.receive_json()` has no per-call timeout, so rather than
        # polling for "nothing else arrives" (unbounded if it never does),
        # bound the wait on a frame that MUST eventually arrive and inspect
        # everything collected along the way.
        vad_gate.speaking = False
        stt_session.queue_response("", True)
        stt_session.final_text = "second utterance"
        conn.send_json(_audio_msg(_pcm16_chunk()))
        frames = _drain_until(conn, {"utterance"}, timeout=10.0)

        assert not any(f["type"] in ("tts_chunk", "tts_start") for f in frames)
        assert frames[-1]["text"] == "second utterance"
