"""Autonomy / exploration freedom — Part 1 of the "Marvi freedom and graph
mind" spec (``docs/superpowers/specs/2026-07-20-marvi-freedom-and-graph-mind-spec.md``).

Marvi acting on its own between prompts: self-directed web research, a
budget that bounds how much of that happens per day, and an ask-user channel
for when a question is worth interrupting for. Every module here is guarded
(never raises to its caller) and consent-tiered per the spec's ground rules —
autonomous *internal* work (research, asking) runs freely within budget, but
is always logged to ``activity.jsonl`` (source ``"autonomy"``) for visibility.
"""

from __future__ import annotations
