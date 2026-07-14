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

    def accept(self, samples) -> None:
        self.accepted.append(list(samples))

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
    monkeypatch.setattr(web_server, "_duplex_make_vad_gate", lambda: gate)
    return gate


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
def full_fakes(stt_session, identify_speaker, instant_reply, tts_chunks, vad_gate, deep_task, warm_lane):
    return {
        "stt": stt_session,
        "identify": identify_speaker,
        "instant": instant_reply,
        "tts": tts_chunks,
        "vad": vad_gate,
        "deep": deep_task,
        "warm": warm_lane,
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
        assert utterance["speaker"] == "owner"
        assert utterance["speaker_name"] == "Shereef"
        # Renderer audio is PCM16; the streaming STT contract is little-endian
        # Float32. Keep speaker-ID audio in PCM16, but convert the STT copy.
        assert struct.unpack_from("<f", stt.accepted_chunks[0])[0] == pytest.approx(100 / 32768)

        frames = _drain_until(conn, {"tts_end"})
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
        assert utterance["speaker"] == "guest"


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
                "default": "deepseek-v4-flash",
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
        assert conn.receive_json() == {"type": "tts_start", "sample_rate": 24000}
        _recv_until(conn, "tts_end")

        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["task_id"] == task_id
        assert deep_result["text"] == "Here's what I found."

        # Deep-result TTS cycle.
        assert conn.receive_json() == {"type": "tts_start", "sample_rate": 24000}
        _recv_until(conn, "tts_end")


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
        activity = _recv_until(conn, "activity", timeout=5.0)
        assert activity["label"] == "Sub-agent is still working"
        result = _recv_until(conn, "deep_result", timeout=10.0)
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
        assert conn.receive_json() == {"type": "tts_start", "sample_rate": 24000}
        _recv_until(conn, "tts_end")

        error = _recv_until(conn, "error", timeout=10.0)
        assert "agent blew up" in error["error"]

        deep_result = _recv_until(conn, "deep_result", timeout=5.0)
        assert deep_result["text"]  # spoken apology, non-empty

        assert conn.receive_json() == {"type": "tts_start", "sample_rate": 24000}
        _recv_until(conn, "tts_end")


def test_non_owner_never_escalates(duplex_client, full_fakes):
    full_fakes["identify"]["label"] = "guest"
    full_fakes["instant"]["deltas"] = ["[ESCALATE] On it."]
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "do something complicated"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")

        # allow_escalation=False is threaded into the fake instant-reply
        # call, but this fake ignores it and still emits the marker text --
        # the important assertion is that the endpoint never emits
        # "escalated" for a guest. Instead the raw marker text streams
        # through as an ordinary reply (the real instant lane wouldn't emit
        # the marker at all once escalation is disabled in its prompt; this
        # test exercises the endpoint's own gating independent of that).
        frame = conn.receive_json()
        seen_types = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            seen_types.append(frame["type"])
            if frame["type"] == "tts_end":
                break
            frame = conn.receive_json()

        assert "escalated" not in seen_types
        assert "deep_result" not in seen_types


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


def test_instant_lane_down_and_non_owner_still_only_errors_once(duplex_client, full_fakes):
    """Non-owner + instant lane down: fallback must NOT escalate to the deep
    agent (never-escalate gating applies to the fallback path too)."""
    full_fakes["identify"]["label"] = "guest"
    full_fakes["instant"]["raises"] = RuntimeError("no provider configured")
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "hello marvi"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))

        _recv_until(conn, "utterance")
        error = _recv_until(conn, "error", timeout=10.0)
        assert error["type"] == "error"

        time.sleep(0.05)
        assert full_fakes["deep"]["calls"] == []


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
        _recv_until(conn, "deep_result", timeout=10.0)

    lines = _read_voice_perf_lines()
    turn_lines = [l for l in lines if " turn " in l]
    assert any("escalated=true" in l for l in turn_lines)


# ---------------------------------------------------------------------------
# TTS synth-ahead pipeline
# ---------------------------------------------------------------------------


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
    assert decoded == ["First sentence.", "Second sentence.", "Third sentence."]
    # seq is monotonically increasing across the whole turn, not reset
    # per-sentence -- proves all three shared one tts_start/tts_end cycle.
    assert [f["seq"] for f in chunks] == [1, 2, 3]


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
