"""Composio smart-sync package for Marvi's subconscious tick.

Modules:
  snapshot_store   -- per-surface JSON snapshots (cursor + last state) under
                       ``~/.hermes/subconscious/snapshots/``, with throttle and
                       exponential-backoff bookkeeping.
  composio_client   -- thin, lazily-imported wrapper over the Composio Python
                       SDK. Composio is an OPTIONAL dependency; nothing in
                       this package imports it at module load time.
  base              -- the tiny delta-fetcher interface + surface registry.
  gmail / github     -- per-surface delta fetchers implementing that interface.

See ``cron/scripts/subconscious_snapshot.py`` for the Contract 1 entry point
that ties these together, and
``docs/superpowers/specs/2026-07-09-marvi-subconscious-presence-design.md``
for the full design (Workstream C).
"""
