"""``hermes composio`` command implementations.

Connects and reports on the Composio-backed account-awareness surfaces
(Gmail, GitHub, ...) that feed Marvi's subconscious sync (Workstream C of the
2026-07-09 subconscious+presence design). The SDK credential lives in the
secret store as ``COMPOSIO_API_KEY``; the separate Connect MCP credential uses
``COMPOSIO_CONSUMER_API_KEY``. Non-secret snapshot settings live under
``composio.surfaces``. The actual delta
fetchers and snapshot cursors live in ``cron/scripts/subconscious/``.

Mirrors the dispatcher pattern used by every other ``hermes_cli`` subcommand
implementation (see ``hermes_cli/mcp_config.py::mcp_command``): one small
router keyed by ``args.composio_action``, handlers below.
"""

from __future__ import annotations

import sys
import webbrowser
from typing import Any, Dict, List, Optional

from hermes_cli.cli_output import (
    print_error,
    print_header,
    print_info,
    print_success,
    print_warning,
    prompt,
)


def composio_command(args: Any) -> None:
    """Main dispatcher for ``hermes composio`` subcommands."""
    action = getattr(args, "composio_action", None)

    handlers = {
        "connect": cmd_composio_connect,
        "list": cmd_composio_list,
        "ls": cmd_composio_list,
    }

    handler = handlers.get(action)
    if handler is None:
        print_error(f"Unknown composio command: {action or '(none)'}")
        print_info("Usage: hermes composio connect <app> | hermes composio list")
        sys.exit(1)
    handler(args)


# ─── connect ──────────────────────────────────────────────────────────────


def _resolve_api_key(explicit: Optional[str], config: Dict[str, Any]) -> Optional[str]:
    """Resolve the Composio API key for `connect`: an explicit --api-key flag
    wins, then whatever's already configured/in-env, then an interactive
    masked prompt. Returns None if we ended up with nothing (non-interactive,
    no flag, nothing configured)."""
    if explicit and explicit.strip():
        return explicit.strip()

    from cron.scripts.subconscious.composio_client import get_api_key

    existing = get_api_key(config)
    if existing:
        return existing

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    entered = prompt("Composio API key", password=True)
    return entered.strip() or None


def cmd_composio_connect(args: Any) -> None:
    """``hermes composio connect <app>`` -- initiate/verify a Composio
    connection for one surface and add it to ``composio.surfaces``."""
    from hermes_cli.config import load_config, read_raw_config, save_config

    app = str(getattr(args, "app", "") or "").strip().lower()
    if not app:
        print_error("Usage: hermes composio connect <app>  (e.g. gmail, github)")
        sys.exit(1)

    from cron.scripts.subconscious.base import known_surfaces

    known = known_surfaces()
    if app not in known:
        print_warning(
            f"'{app}' has no delta-fetcher implemented yet in this build "
            f"(implemented surfaces: {', '.join(known)}). Marvi will still store "
            f"the connection, but subconscious sync won't watch it until a fetcher "
            f"ships for it."
        )

    config = load_config()
    api_key = _resolve_api_key(getattr(args, "api_key", None), config)
    if not api_key:
        print_error(
            "No Composio API key available. Pass --api-key, set it interactively, "
            "or export COMPOSIO_API_KEY."
        )
        sys.exit(1)

    from cron.scripts.subconscious.composio_client import (
        ComposioAuthError,
        ComposioClient,
        ComposioRateLimited,
        ComposioTransientError,
        ComposioUnavailable,
        ensure_sdk_installed,
        is_sdk_installed,
    )

    if not is_sdk_installed():
        print_info("Composio SDK not installed -- installing it now...")
        try:
            ensure_sdk_installed(prompt=True)
        except ComposioUnavailable as e:
            print_error("Composio SDK not installed.")
            print_info(str(e))
            sys.exit(1)
        print_success("Composio SDK installed.")

    client = ComposioClient(api_key)
    try:
        client.verify_auth()
    except ComposioAuthError as e:
        print_error(f"Composio rejected this API key: {e}")
        sys.exit(1)
    except (ComposioRateLimited, ComposioTransientError) as e:
        print_warning(
            f"Could not verify the Composio API key right now ({e}); saving it anyway."
        )
    except Exception as e:  # pragma: no cover - defensive
        print_warning(
            f"Unexpected error verifying the Composio API key ({e}); saving it anyway."
        )

    redirect_url = None
    try:
        result = client.initiate_connection(app)
        status = str(result.get("status") or "").lower()
        redirect_url = result.get("redirect_url") or result.get("redirectUrl")
        if redirect_url:
            print_info(f"Open this link to finish authorizing {app}:")
            print_info(f"  {redirect_url}")
            try:
                opened = webbrowser.open(redirect_url)
            except Exception:
                opened = False
            if opened:
                print_success("Opened the authorization link in your browser.")
        elif status:
            print_info(f"Composio connection status for {app}: {status}")
    except ComposioAuthError as e:
        print_error(f"Composio rejected this API key while connecting {app}: {e}")
        sys.exit(1)
    except (ComposioRateLimited, ComposioTransientError, ComposioUnavailable) as e:
        print_warning(
            f"Could not initiate the Composio connection for {app} right now ({e})."
        )
        print_info(
            "The surface will still be added to your config; retry `hermes composio list` later."
        )
    except Exception as e:  # pragma: no cover - defensive
        print_warning(
            f"Unexpected error initiating the Composio connection for {app} ({e})."
        )

    # Persist the credential in .env, configure the official Composio Connect
    # MCP server, and keep only non-secret snapshot settings in config.yaml.
    from hermes_cli.composio_config import configure_composio_connect

    configure_composio_connect(
        api_key=api_key,
        consumer_api_key=getattr(args, "consumer_api_key", None),
    )
    config = read_raw_config()
    composio_cfg = config.setdefault("composio", {})
    if not isinstance(composio_cfg, dict):
        composio_cfg = {}
        config["composio"] = composio_cfg

    surfaces = composio_cfg.get("surfaces")
    if not isinstance(surfaces, list):
        surfaces = []
    if app not in surfaces:
        surfaces.append(app)
    composio_cfg["surfaces"] = surfaces

    save_config(config)

    print_success(f"Marvi is now set up to watch '{app}' via Composio.")
    if redirect_url:
        print_info(
            "Finish the authorization link above, then check `hermes composio list`."
        )


# ─── list ─────────────────────────────────────────────────────────────────


def _surface_auth_status(app: str, config: Dict[str, Any]) -> str:
    from cron.scripts.subconscious.composio_client import (
        ComposioAuthError,
        ComposioRateLimited,
        ComposioTransientError,
        ComposioUnavailable,
        get_api_key,
        get_client,
        is_sdk_installed,
    )

    if not is_sdk_installed():
        return "sdk not installed"
    if not get_api_key(config):
        return "no api key"
    try:
        client = get_client()
        status = client.get_connection_status(app)
        return (
            "connected"
            if status.get("connected")
            else f"not connected ({status.get('status')})"
        )
    except ComposioAuthError:
        return "auth error"
    except ComposioRateLimited:
        return "rate limited"
    except (ComposioTransientError, ComposioUnavailable) as e:
        return f"unavailable ({e})"
    except Exception as e:  # pragma: no cover - defensive
        return f"error ({e})"


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def cmd_composio_list(args: Any) -> None:
    """``hermes composio list`` -- surfaces, auth status, last sync, cursor age."""
    from hermes_cli.config import load_config

    config = load_config()
    composio_cfg = config.get("composio") or {}
    surfaces: List[str] = []
    if isinstance(composio_cfg, dict) and isinstance(
        composio_cfg.get("surfaces"), list
    ):
        surfaces = [
            str(s).strip().lower()
            for s in composio_cfg["surfaces"]
            if str(s or "").strip()
        ]

    print_header("Marvi account-awareness surfaces (Composio)")

    if not surfaces:
        print_info(
            "No surfaces connected yet. Run `hermes composio connect gmail` (or github) to add one."
        )
        return

    from cron.scripts.subconscious.snapshot_store import open_store

    for app in surfaces:
        auth_status = _surface_auth_status(app, config)
        try:
            store = open_store(app)
            status = store.status_dict()
        except Exception as e:  # pragma: no cover - defensive
            print_warning(f"{app}: could not read snapshot store ({e})")
            continue

        last_sync = _format_age(status["seconds_since_last_fetch"])
        line = f"  {app:<10} auth={auth_status:<24} last_sync={last_sync}"
        if status["consecutive_failures"]:
            line += f"  failures={status['consecutive_failures']}"
            if status["next_retry_at"]:
                line += f"  retry_at={status['next_retry_at']}"
        print(line)
