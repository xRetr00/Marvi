from agent.platform_tools import filter_platform_tools


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def test_show_card_is_visible_to_desktop_and_voice_agents():
    tools = [_tool("web_search"), _tool("show_card")]

    assert filter_platform_tools(tools, "desktop") == tools
    assert filter_platform_tools(tools, "voice") == tools
    assert filter_platform_tools(tools, "voice-subagent") == tools
    assert [tool["function"]["name"] for tool in filter_platform_tools(tools, "telegram")] == ["web_search"]
