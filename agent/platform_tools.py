"""Platform-only model tool visibility rules."""

from typing import Any


def filter_platform_tools(tools: list[dict[str, Any]], platform: str | None) -> list[dict[str, Any]]:
    """Expose presentation cards only where a renderer consumes them."""
    platform = str(platform or "")
    if platform == "desktop" or platform.startswith("voice"):
        return tools

    return [tool for tool in tools if tool.get("function", {}).get("name") != "show_card"]
