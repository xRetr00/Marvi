"""Regression tests for the Qwen/DashScope chat-message preparation.

DashScope rejects a content array that contains an empty-text part (or an empty
array) with ``messages.N.content: Invalid input``. This happens when the model
calls a tool with little/no visible text (e.g. ``show_card`` in voice mode), so
the prep must never emit an empty-text part and must omit content for a
tool-call-only assistant turn.
"""

from __future__ import annotations

from run_agent import AIAgent


def _agent() -> AIAgent:
    # Pure-method tests: skip the heavy __init__/provider setup.
    return object.__new__(AIAgent)


def test_tool_call_with_empty_text_omits_content():
    agent = _agent()
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "show_card", "arguments": "{}"}}],
        }
    ]
    out = agent._qwen_prepare_chat_messages(msgs)
    assert out[0].get("content") is None
    assert out[0]["tool_calls"]


def test_whitespace_only_text_does_not_become_empty_part():
    agent = _agent()
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "   "}, {"type": "text", "text": "hi"}],
            "tool_calls": [{"id": "c1"}],
        }
    ]
    out = agent._qwen_prepare_chat_messages(msgs)
    text_parts = [p for p in out[0]["content"] if isinstance(p, dict) and p.get("type") == "text"]
    assert text_parts == [{"type": "text", "text": "hi"}]


def test_normal_text_is_wrapped():
    agent = _agent()
    out = agent._qwen_prepare_chat_messages([{"role": "user", "content": "hello"}])
    assert out[0]["content"] == [{"type": "text", "text": "hello"}]


def test_image_part_is_preserved():
    agent = _agent()
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    out = agent._qwen_prepare_chat_messages(msgs)
    assert out[0]["content"] == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]


def test_inplace_variant_also_omits_empty_tool_call_content():
    agent = _agent()
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}]
    agent._qwen_prepare_chat_messages_inplace(msgs)
    assert msgs[0].get("content") is None
