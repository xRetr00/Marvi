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

import base64
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
    web_server.app.state.auth_required = False
    web_server.app.state.bound_host = None

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


@pytest.fixture
def stt_session(monkeypatch):
    session = FakeSttSession()
    monkeypatch.setattr(web_server, "_duplex_stt_session", lambda stt_cfg: session)
    return session


@pytest.fixture
def identify_speaker(monkeypatch):
    state = {"label": "owner", "score": 0.9}

    def fake_identify(pcm16_bytes):
        return state["label"], state["score"]

    monkeypatch.setattr(web_server, "_duplex_identify_speaker", fake_identify)
    return state


@pytest.fixture
def instant_reply(monkeypatch):
    state = {"deltas": ["Hi", " there."], "raises": None, "delay": 0.0}

    def fake_stream(transcript, utterance, *, allow_escalation):
        if state["raises"] is not None:
            raise state["raises"]
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
    state = {"text": "Deep answer.", "raises": None, "calls": []}

    def fake_run(transcript_messages, task_text):
        state["calls"].append(task_text)
        if state["raises"] is not None:
            raise state["raises"]
        return state["text"]

    monkeypatch.setattr(web_server, "_duplex_run_deep_task", fake_run)
    return state


@pytest.fixture
def full_fakes(stt_session, identify_speaker, instant_reply, tts_chunks, vad_gate, deep_task):
    return {
        "stt": stt_session,
        "identify": identify_speaker,
        "instant": instant_reply,
        "tts": tts_chunks,
        "vad": vad_gate,
        "deep": deep_task,
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
        assert any(frame["type"] == "tts_start" for frame in frames)

    # STT was re-armed for the next utterance immediately after finishing.
    assert stt.begin_count >= 2


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
        task_id = escalated["task_id"]
        assert task_id

        # Ack TTS cycle.
        assert conn.receive_json() == {"type": "tts_start"}
        _recv_until(conn, "tts_end")

        deep_result = _recv_until(conn, "deep_result", timeout=10.0)
        assert deep_result["task_id"] == task_id
        assert deep_result["text"] == "Here's what I found."

        # Deep-result TTS cycle.
        assert conn.receive_json() == {"type": "tts_start"}
        _recv_until(conn, "tts_end")


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
        assert conn.receive_json() == {"type": "tts_start"}
        _recv_until(conn, "tts_end")

        error = _recv_until(conn, "error", timeout=10.0)
        assert "agent blew up" in error["error"]

        deep_result = _recv_until(conn, "deep_result", timeout=5.0)
        assert deep_result["text"]  # spoken apology, non-empty

        assert conn.receive_json() == {"type": "tts_start"}
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
    full_fakes["instant"]["deltas"] = ["This ", "is ", "a ", "long ", "reply ", "that ", "keeps ", "going."]
    full_fakes["instant"]["delay"] = 0.05
    stt = full_fakes["stt"]
    stt.queue_response("", True)
    stt.final_text = "tell me a long story"

    with duplex_client.websocket_connect(_duplex_url()) as conn:
        conn.receive_json()  # ready
        conn.send_json(_audio_msg(_pcm16_chunk()))
        _recv_until(conn, "utterance")

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
