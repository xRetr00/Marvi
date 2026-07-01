from agent.agent_runtime_helpers import sanitize_api_messages


def test_sanitize_api_messages_stringifies_decoded_tool_content_dict():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "show_card"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": {"success": True}},
    ]

    sanitized = sanitize_api_messages(messages)

    assert sanitized[1]["content"] == '{"success": true}'


def test_sanitize_api_messages_strips_nul_from_string_content():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {"name": "show_card"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "\x00json:{\"success\": true}"},
    ]

    sanitized = sanitize_api_messages(messages)

    assert sanitized[1]["content"] == 'json:{"success": true}'
