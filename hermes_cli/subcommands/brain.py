"""``hermes brain`` parser."""

from __future__ import annotations

from typing import Callable


def build_brain_parser(subparsers, *, cmd_brain: Callable) -> None:
    parser = subparsers.add_parser("brain", help="Manage private local full-text recall")
    sub = parser.add_subparsers(dest="brain_command")
    enable = sub.add_parser("enable", help="Enable indexing for one or more folders")
    enable.add_argument("folders", nargs="+")
    sub.add_parser("disable", help="Disable scheduled indexing")
    sub.add_parser("status", help="Show index status")
    sub.add_parser("index", help="Index changed files now")
    search = sub.add_parser("search", help="Search the local index")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=8)
    parser.set_defaults(func=cmd_brain)
