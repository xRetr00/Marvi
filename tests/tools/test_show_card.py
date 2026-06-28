from unittest.mock import patch

from tools import show_card


def test_show_card_emits_event_and_returns_ok():
    captured = {}

    def fake_emit(session_key, event):
        captured["session"] = session_key
        captured["event"] = event
        return True

    with patch("tools.show_card.get_current_session_key", return_value="s1"), patch(
        "tools.show_card.emit_ui_event", side_effect=fake_emit
    ):
        result = show_card.handle_show_card({"title": "Done", "body": "Shipped it", "kind": "result"})

    assert result["success"] is True
    assert captured["session"] == "s1"
    assert captured["event"]["event"] == "card.show"
    assert captured["event"]["payload"]["body"] == "Shipped it"
    assert captured["event"]["payload"]["kind"] == "result"


def test_show_card_reports_when_no_client():
    with patch("tools.show_card.get_current_session_key", return_value="s1"), patch(
        "tools.show_card.emit_ui_event", return_value=False
    ):
        result = show_card.handle_show_card({"body": "hi"})

    assert result["success"] is False


def test_show_card_requires_body():
    result = show_card.handle_show_card({})
    assert result["success"] is False
