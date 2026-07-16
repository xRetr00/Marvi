"""Marvi's episodic memory tier (Loop 1 of the memory-maturity round).

A structured, time-indexed log of what actually happened — distinct from
the curated semantic memory in ``tools/memory_tool.py`` (USER.md/MEMORY.md).
See ``docs/superpowers/specs/2026-07-17-marvi-memory-maturity-spec.md``.
"""

from .episodic import (
    VALID_ACTORS,
    VALID_KINDS,
    count,
    episodic_config,
    format_episode,
    purge_before,
    query,
    record_episode,
    recent,
)

__all__ = [
    "VALID_ACTORS",
    "VALID_KINDS",
    "count",
    "episodic_config",
    "format_episode",
    "purge_before",
    "query",
    "record_episode",
    "recent",
]
