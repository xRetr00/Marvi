"""Secret-safe Composio Connect configuration shared by CLI and Desktop.

The credential lives in ``.env``.  ``config.yaml`` stores only the stable MCP
endpoint plus an environment-variable reference, so Composio's complete tool
catalog is available without adding another core model tool.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


COMPOSIO_ENV_KEY = "COMPOSIO_API_KEY"
COMPOSIO_MCP_NAME = "composio"
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"


def _mcp_entry() -> Dict[str, Any]:
    return {
        "url": COMPOSIO_MCP_URL,
        "headers": {"x-consumer-api-key": f"${{{COMPOSIO_ENV_KEY}}}"},
        "enabled": True,
    }


def configure_composio_connect(api_key: Optional[str] = None) -> Dict[str, Any]:
    """Persist ``api_key`` as a secret and enable the official Connect MCP.

    Also migrates the legacy ``composio.api_key`` value out of config.yaml.
    The operation is idempotent and preserves ``composio.surfaces`` (the small
    set of account snapshots used by the proactive delta poller).
    """
    from hermes_cli.config import get_env_value_prefer_dotenv, read_raw_config, save_config, save_env_value

    config = read_raw_config() or {}
    composio_cfg = config.get("composio")
    if not isinstance(composio_cfg, dict):
        composio_cfg = {}

    legacy = composio_cfg.pop("api_key", None)
    resolved = str(api_key or legacy or get_env_value_prefer_dotenv(COMPOSIO_ENV_KEY) or "").strip()
    if resolved:
        save_env_value(COMPOSIO_ENV_KEY, resolved)

    if composio_cfg:
        config["composio"] = composio_cfg
    else:
        config.pop("composio", None)

    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[COMPOSIO_MCP_NAME] = _mcp_entry()
    config["mcp_servers"] = servers
    save_config(config)

    return {
        "configured": bool(resolved),
        "mcp_enabled": True,
        "migrated_legacy_key": bool(legacy),
    }


def composio_status(*, migrate: bool = False) -> Dict[str, Any]:
    """Return non-secret Composio/MCP state."""
    from hermes_cli.config import get_env_value_prefer_dotenv, load_config

    config = load_config()
    composio_cfg = config.get("composio")
    legacy = composio_cfg.get("api_key") if isinstance(composio_cfg, dict) else None
    if migrate and legacy:
        configure_composio_connect()
        config = load_config()
        composio_cfg = config.get("composio")
        legacy = composio_cfg.get("api_key") if isinstance(composio_cfg, dict) else None

    servers = config.get("mcp_servers")
    entry = servers.get(COMPOSIO_MCP_NAME) if isinstance(servers, dict) else None
    configured = bool(get_env_value_prefer_dotenv(COMPOSIO_ENV_KEY) or legacy)
    return {
        "configured": configured,
        "mcp_enabled": bool(isinstance(entry, dict) and entry.get("enabled", True) is not False),
        "mcp_url": entry.get("url") if isinstance(entry, dict) else None,
        "legacy_key_present": bool(legacy),
        "snapshot_surfaces": list(composio_cfg.get("surfaces") or []) if isinstance(composio_cfg, dict) else [],
    }
