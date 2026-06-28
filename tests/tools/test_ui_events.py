from tools import ui_events


def test_emit_routes_to_registered_session_callback():
    received = []
    ui_events.register_ui_event_notify("sess-1", lambda evt: received.append(evt))
    try:
        ok = ui_events.emit_ui_event("sess-1", {"event": "card.show", "payload": {"body": "hi"}})
    finally:
        ui_events.unregister_ui_event_notify("sess-1")

    assert ok is True
    assert received == [{"event": "card.show", "payload": {"body": "hi"}}]


def test_emit_is_noop_without_listener():
    assert ui_events.emit_ui_event("nobody", {"event": "card.show"}) is False
