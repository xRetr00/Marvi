"""Platform-only model tool visibility rules."""

from typing import Any


def filter_platform_tools(tools: list[dict[str, Any]], platform: str | None) -> list[dict[str, Any]]:
    """Keep voice presentation tools out of non-voice agent prompts."""
    if str(platform or "").startswith("voice"):
        return tools

    return [tool for tool in tools if tool.get("function", {}).get("name") != "show_card"]
