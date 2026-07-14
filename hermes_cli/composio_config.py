"""Secret-safe Composio Connect configuration shared by CLI and Desktop.

The credentials live in ``.env``. ``config.yaml`` stores only the stable MCP
endpoint plus an environment-variable reference, so Composio's complete tool
catalog is available without adding another core model tool.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


COMPOSIO_ENV_KEY = "COMPOSIO_API_KEY"
COMPOSIO_CONSUMER_ENV_KEY = "COMPOSIO_CONSUMER_API_KEY"
COMPOSIO_MCP_NAME = "composio"
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"


def _mcp_entry(*, enabled: bool) -> Dict[str, Any]:
    return {
        "url": COMPOSIO_MCP_URL,
        "headers": {"x-consumer-api-key": f"${{{COMPOSIO_CONSUMER_ENV_KEY}}}"},
        "enabled": enabled,
    }


def configure_composio_connect(
    api_key: Optional[str] = None,
    consumer_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist the distinct SDK and Connect MCP secrets.

    Also migrates the legacy ``composio.api_key`` value out of config.yaml.
    The operation is idempotent and preserves ``composio.surfaces`` (the small
    set of account snapshots used by the proactive delta poller).
    """
    from hermes_cli.config import (
        get_env_value_prefer_dotenv,
        read_raw_config,
        save_config,
        save_env_value,
    )

    config = read_raw_config() or {}
    composio_cfg = config.get("composio")
    if not isinstance(composio_cfg, dict):
        composio_cfg = {}

    legacy = composio_cfg.pop("api_key", None)
    sdk_key = str(
        api_key or legacy or get_env_value_prefer_dotenv(COMPOSIO_ENV_KEY) or ""
    ).strip()
    consumer_key = str(
        consumer_api_key or get_env_value_prefer_dotenv(COMPOSIO_CONSUMER_ENV_KEY) or ""
    ).strip()
    if sdk_key:
        save_env_value(COMPOSIO_ENV_KEY, sdk_key)
    if consumer_key:
        save_env_value(COMPOSIO_CONSUMER_ENV_KEY, consumer_key)

    if composio_cfg:
        config["composio"] = composio_cfg
    else:
        config.pop("composio", None)

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[COMPOSIO_MCP_NAME] = _mcp_entry(enabled=bool(consumer_key))
    config["mcp_servers"] = servers
    save_config(config)

    return {
        "configured": bool(sdk_key),
        "sdk_configured": bool(sdk_key),
        "mcp_configured": bool(consumer_key),
        "mcp_enabled": bool(consumer_key),
        "migrated_legacy_key": bool(legacy),
    }


def composio_status(*, migrate: bool = False) -> Dict[str, Any]:
    """Return non-secret Composio/MCP state."""
    from hermes_cli.config import (
        get_env_value_prefer_dotenv,
        load_config,
        read_raw_config,
    )

    config = load_config()
    composio_cfg = config.get("composio")
    legacy = composio_cfg.get("api_key") if isinstance(composio_cfg, dict) else None
    raw_servers = (read_raw_config() or {}).get("mcp_servers")
    raw_entry = (
        raw_servers.get(COMPOSIO_MCP_NAME) if isinstance(raw_servers, dict) else None
    )
    raw_headers = raw_entry.get("headers") if isinstance(raw_entry, dict) else None
    old_mcp_key = (
        isinstance(raw_headers, dict)
        and raw_headers.get("x-consumer-api-key") == f"${{{COMPOSIO_ENV_KEY}}}"
    )
    if migrate and (legacy or old_mcp_key):
        configure_composio_connect()
        config = load_config()
        composio_cfg = config.get("composio")
        legacy = composio_cfg.get("api_key") if isinstance(composio_cfg, dict) else None

    servers = config.get("mcp_servers")
    entry = servers.get(COMPOSIO_MCP_NAME) if isinstance(servers, dict) else None
    sdk_configured = bool(get_env_value_prefer_dotenv(COMPOSIO_ENV_KEY) or legacy)
    mcp_configured = bool(get_env_value_prefer_dotenv(COMPOSIO_CONSUMER_ENV_KEY))
    return {
        "configured": sdk_configured,
        "sdk_configured": sdk_configured,
        "mcp_configured": mcp_configured,
        "mcp_enabled": bool(
            mcp_configured
            and isinstance(entry, dict)
            and entry.get("enabled", True) is not False
        ),
        "mcp_url": entry.get("url") if isinstance(entry, dict) else None,
        "legacy_key_present": bool(legacy),
        "snapshot_surfaces": list(composio_cfg.get("surfaces") or [])
        if isinstance(composio_cfg, dict)
        else [],
    }
