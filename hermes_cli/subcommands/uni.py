"""``hermes uni`` subcommand parser — Duzce student-portal enrollment
(Marvi freedom spec §1.3).

Handler injected to avoid importing ``main``, matching every other
subcommand module here (e.g. ``auth.py``, ``composio.py``).
"""

from __future__ import annotations

from typing import Callable


def build_uni_parser(subparsers, *, cmd_uni: Callable) -> None:
    """Attach the ``uni`` subcommand to ``subparsers``."""
    uni_parser = subparsers.add_parser(
        "uni",
        help="Enroll/manage the Duzce University student-portal check (uni_portal plugin)",
        description=(
            "Enroll Marvi to watch your Duzce University student portal for new "
            "grades and announcements. Credentials are captured interactively and "
            "stored in the Windows Credential Manager -- never in config.yaml or "
            "in any file the agent's own memory/logging touches. See "
            "plugins/uni_portal/SKILL.md for the full security boundary."
        ),
    )
    uni_subparsers = uni_parser.add_subparsers(dest="uni_action")

    login_p = uni_subparsers.add_parser(
        "login",
        help="Enroll (prompts for username/password) or remove stored credentials",
    )
    login_p.add_argument(
        "--username", help="Student-system username (otherwise prompted)"
    )
    login_p.add_argument(
        "--logout",
        action="store_true",
        help="Remove stored credentials and disable the daily check instead of enrolling",
    )

    uni_subparsers.add_parser("status", help="Show enrollment + last daily-check status")
    uni_subparsers.add_parser("check", help="Run today's check immediately")

    uni_parser.set_defaults(func=cmd_uni)
