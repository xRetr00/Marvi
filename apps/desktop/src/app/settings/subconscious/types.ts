// Shared types for the Subconscious settings surface (Marvi's proactive
// subconscious/presence/goals/accounts UI). Config field names mirror
// Contract 3 in docs/superpowers/specs/2026-07-09-marvi-subconscious-presence-design.md
// exactly (subconscious.*, presence.*, composio.*) so this UI keeps working
// unmodified once the backend workstreams (A/B/C) land their schema entries —
// the desktop config channel (`GET`/`PUT /api/config`) deep-merges arbitrary
// keys today, schema or not.

/** A single proactivity tier assignable per suggestion category. */
export type SubconsciousTier = 'notify' | 'propose' | 'auto'

/** Map of suggestion category -> proactivity tier (`subconscious.tiers`). */
export type TierMap = Record<string, SubconsciousTier>

/**
 * A goal in ``~/.hermes/goals.json`` (owned by Workstream A's
 * agent/goal_store.py). Field names mirror the spec's Contract exactly:
 * id, title, detail, status, created, updated, horizon.
 */
export interface Goal {
  id: string
  title: string
  detail: string
  status: GoalStatus
  horizon: GoalHorizon
  created: string
  updated: string
}

export type GoalStatus = 'active' | 'paused' | 'done'
export type GoalHorizon = 'short' | 'long'

/** A distilled presence/subconscious memory entry for "What Marvi knows". */
export interface KnowledgeEntry {
  id: string
  summary: string
  source: 'presence' | 'subconscious'
  createdAt: string
}
