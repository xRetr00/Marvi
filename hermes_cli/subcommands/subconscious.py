"""``hermes subconscious`` subcommand parser.

Mirrors ``hermes_cli/subcommands/cron.py``'s structure: the parser lives
here, the handler is injected so this module never imports ``main``
(cycle avoidance).
"""

from __future__ import annotations

from typing import Callable


def build_subconscious_parser(subparsers, *, cmd_subconscious: Callable) -> None:
    """Attach the ``subconscious`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "subconscious",
        help="Proactive world-diff + goal-aware background reasoning",
        description=(
            "Manage the subconscious tick — a single built-in cron job that "
            "periodically diffs the user's world, reasons over active goals "
            "and memory, and stays silent unless something is worth "
            "surfacing or proposing."
        ),
    )
    sub = parser.add_subparsers(dest="subconscious_command")

    sub.add_parser("status", help="Show whether the subconscious tick is enabled and its last/next run")

    enable_parser = sub.add_parser("enable", help="Enable the subconscious tick")
    enable_parser.add_argument(
        "--interval",
        help="Tick cadence, e.g. '20m' or '1h' (default: 20m, or subconscious.interval in config.yaml)",
    )

    sub.add_parser("disable", help="Disable the subconscious tick (pauses the job, keeps config)")

    parser.set_defaults(func=cmd_subconscious)
