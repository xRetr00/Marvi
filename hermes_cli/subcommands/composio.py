"""``hermes composio`` subcommand parser.

Marvi's account-awareness surfaces (Gmail, GitHub, ...) connect through
Composio -- see Contract 3 / Workstream C in
``docs/superpowers/specs/2026-07-09-marvi-subconscious-presence-design.md``.
Handler injected to avoid importing ``main``, matching every other
subcommand module here (e.g. ``mcp.py``).
"""

from __future__ import annotations

from typing import Callable


def build_composio_parser(subparsers, *, cmd_composio: Callable) -> None:
    """Attach the ``composio`` subcommand to ``subparsers``."""
    composio_parser = subparsers.add_parser(
        "composio",
        help="Connect account-awareness surfaces (Gmail, GitHub, ...) via Composio",
        description=(
            "Manage the Composio-backed account surfaces that feed Marvi's "
            "subconscious sync. Every sync tick is a delta fetch against a "
            "locally stored cursor -- never a blind full refetch.\n\n"
            "Use 'hermes composio connect <app>' to connect a surface, or "
            "'hermes composio list' to see connection status and sync freshness."
        ),
    )
    composio_sub = composio_parser.add_subparsers(dest="composio_action")

    connect_p = composio_sub.add_parser(
        "connect",
        help="Connect (or re-verify) a Composio surface, e.g. gmail or github",
    )
    connect_p.add_argument("app", help="Surface/app name, e.g. gmail, github")
    connect_p.add_argument(
        "--api-key",
        help="Composio API key (otherwise prompts interactively, or reads COMPOSIO_API_KEY)",
    )
    connect_p.add_argument(
        "--consumer-api-key",
        help="AI Clients key for Connect MCP (otherwise reads COMPOSIO_CONSUMER_API_KEY)",
    )

    composio_sub.add_parser(
        "list",
        aliases=["ls"],
        help="List connected surfaces, auth status, and sync freshness",
    )

    composio_parser.set_defaults(func=cmd_composio)
