from agent.platform_tools import filter_platform_tools


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def test_show_card_is_visible_only_to_voice_agents():
    tools = [_tool("web_search"), _tool("show_card")]

    assert [tool["function"]["name"] for tool in filter_platform_tools(tools, "desktop")] == ["web_search"]
    assert filter_platform_tools(tools, "voice") == tools
    assert filter_platform_tools(tools, "voice-subagent") == tools
