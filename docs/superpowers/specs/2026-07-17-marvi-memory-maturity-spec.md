# Marvi Memory Maturity — Episodic Memory, Dreaming, Decay, Adaptive Recall

Date: 2026-07-17 · Implementer: Codex (loops 2-4) · Loop 1 built by Claude's agent · Branch: main

## Vision

Marvi has semantic memory (USER.md/MEMORY.md + topic tree), procedural memory
(self-improving skills), an inner narrative, nightly reflection, and learning
loops. The missing tier is **episodic memory** — a queryable, time-indexed log
of what actually happened — plus the consolidation, decay, and adaptive
retrieval that keep a growing memory sharp instead of bloated. This is the
"memory maturity" round: four loops that make Marvi remember like a person —
events, not just facts; weekly consolidation, not just daily notes; forgetting,
not only adding; and retrieval that learns what's useful.

Aligned with 2026 practice: three-tier memory (episodic/semantic/procedural),
Reflective Memory Management (prospective + retrospective), and "dreaming"
(offline cross-session consolidation that promotes only threshold-crossing
signals). Governed by the proactivity mandate: episodic recall makes the
subconscious ask "has this happened before?" — the single biggest reasoning
upgrade available.

## Ground rules (read first)

- **Marvi** in user-visible strings; code identifiers may say hermes.
- `cfg_get(cfg, ...)` config; DEFAULT_CONFIG only for UI-edited keys.
- Endpoints in `hermes_cli/web_server.py`, sync work via `run_in_threadpool`
  (the `[LOOP-LAG]` watchdog flags event-loop blocking).
- Storage: atomic tempfile+replace, 0600, in-process lock (mirror
  `cron/suggestions.py`). New stores under `HERMES_HOME/memory/`.
- Consent-first: any memory CHANGE the user would care about (a merge that
  drops a fact, a promoted preference) that isn't obviously safe flows through
  `cron/suggestions.py`; pure internal reorganization (dedup, decay scoring)
  runs autonomously but is logged to activity.jsonl.
- Prompt house style: plain prose, explicit contracts (see
  `tools/presence/goblin.py`, `DISTILL_SYSTEM_NOTE`).
- Anchors that exist today: `tools/memory_tool.py` (MemoryStore, ENTRY_DELIMITER
  = "\n§\n", flat USER.md/MEMORY.md under HERMES_HOME/memories/, topic paths via
  split_topic); `cron/scheduler.py` (activity.jsonl writer:
  `_append_activity_record(record)`, `_append_subconscious_activity(job,
  outcome, diff=, thought=)`, `_subconscious_activity_path()`,
  `_rotate_subconscious_activity`); the reflection job in `cron/subconscious.py`
  ("Subconscious reflection", ~03:30) and `agent/learning/reflection.py`;
  `tools/presence/distill.py` (`build_digest`, `print_digest_for_cron`);
  session FTS search (find it — used by session_search toolset).
- Unit-test every loop with fakes; learned/consolidated changes reproducible
  from fixtures. Known pre-existing failures: Windows chmod tests in tests/cron;
  jsdom "document is not defined" in some desktop suites — baseline with git
  stash before claiming breakage.
- Installed copy runs from `%LOCALAPPDATA%\hermes\hermes-agent`; ship in repo.
- Codex may hold uncommitted WIP (plugins/smart_room/**, some voice/desktop
  files) — re-check git status before finishing; never revert others' work.

---

## Loop 1 — Episodic memory store + recall (FOUNDATION; Claude's agent builds this now)

**Goal:** a structured, time-indexed record of events Marvi can query —
"what did I do last Tuesday", "when did I last touch NeuDocs", "have I seen
this error before".

### 1.1 Store — `agent/memory/episodic.py`
SQLite at `HERMES_HOME/memory/episodic.db` (mirror the document brain's FTS5
schema in `tools/brain/store.py` — same dependency-free approach, no vectors).
Tables:
- `episodes(id, ts, kind, actor, title, summary, source, ref, entities_json,
  importance REAL, created_at)` — kind ∈ conversation|task|room|proactive|
  device|arrival|learning; actor ∈ marvi|user|world; importance 0..1.
- `episodes_fts` (FTS5 over title+summary+entities) for text recall.
- Index on `ts` for time-range queries.

Public API (all thread-safe, never raise to caller):
`record_episode(kind, title, summary, *, actor="marvi", source, ref=None,
entities=None, importance=0.5, ts=None) -> id`;
`query(*, text=None, kind=None, since=None, until=None, entities=None,
limit=20) -> list[Episode]` (FTS when text given, else time/kind filter,
newest-first, importance as tiebreaker);
`recent(limit=20)`, `count()`, `purge_before(ts)`.

### 1.2 Ingestion — populate WITHOUT new cost
Episodes are DERIVED from signals already produced, via one guarded hook each
(best-effort, never blocks):
- **Activity feed** (primary): in `cron/scheduler.py` where
  `_append_subconscious_activity` writes a record, ALSO call
  `record_episode(kind=<mapped from source>, ...)` for meaningful outcomes
  (message/suggestion/world/goblin — NOT no_change). Map source→kind.
- **Conversations**: at session end (find the session-end hook the
  background-review/skill loop uses in `agent/background_review.py` or gateway),
  record one episode summarizing the session (reuse the summary the
  session-search/compression already computes — do NOT add an LLM call just for
  this; if a cheap summary exists, use it, else title-only from first user msg).
- **Distiller**: `tools/presence/distill.py` already digests the day — have its
  cron completion record a `kind=room`/`task` episode from the digest.
- **Learning proposals accepted/dismissed**: `kind=learning` episode.
Ingestion is additive-only and idempotent by `(source, ref)` (skip duplicates).

### 1.3 Recall tool — `tools/episodic_tool.py`
Register `recall_episode` via `tools.registry` (toolset `memory`, mirror
`tools/brain_tool.py` registration — use `from tools.registry import registry`
then `registry.register(...)`; NOTE the module-vs-instance import bug that hit
brain_tool — the smoke test `tests/tools/test_tool_registration_smoke.py`
guards it, run it). Params: `{query?, kind?, since?, until?, limit?}` →
formatted episode list (time, kind, title, summary). Add to the instant lane's
read-only whitelist (`tools/voice_instant_lane.py`) and the subconscious/
reflection toolsets so background thinking can ask "has this happened before?".

### 1.4 Subconscious integration
- Reflection prompt input gains a compact "recent episodes" block (last ~15
  meaningful episodes) so nightly thinking reasons over the day's events, not
  just diffs.
- The tick's stage-2 prompt may call `recall_episode` when a diff resembles a
  past situation.

### 1.5 UI — Mind page "Timeline" tab (apps/desktop/src/app/mind/)
The Mind page already has tabs (overview/noticed/goals/brain/knowledge/composio
— see index.tsx). Add a **Timeline** tab: `GET /api/memory/episodes?since=&
kind=&q=&limit=` renders a reverse-chronological, filterable event stream
(day groupings, kind chips, search box). This is "Marvi's diary" — the most
tangible proof the subconscious is alive. Empty state: "Marvi's episodic
memory starts filling as it observes your days."

### 1.6 Config
`memory.episodic.{enabled(default true), retain_days(default 400),
min_importance_for_prompt(default 0.4)}`.

### 1.7 Tests
Store CRUD + FTS recall + time-range; ingestion idempotency (same source/ref
once); source→kind mapping; recall tool registration (smoke) + formatting;
reflection input includes episodes; retain/purge. Fakes only, no real LLM.

---

## Loop 2 — Dreaming: weekly cross-session consolidation (Codex)

**Goal:** beyond nightly reflection (today-scoped), a weekly offline sweep over
accumulated episodes + past sessions that finds repeated patterns, promotes
durable insights, and reconciles the memory — the "dreaming" pattern.

- New cron job `Subconscious dreaming`, weekly (config
  `memory.dreaming.schedule`, default `0 4 * * 0` — Sunday 04:00, after
  reflection), created idempotently by `cron/subconscious.py::enable()` (add
  alongside the reflection job; disable removes/pauses all three).
- Inputs (bounded): last 7 days of episodes (Loop 1), session summaries via
  session FTS, current narrative, semantic memory (USER.md/MEMORY.md), the
  outcomes ledger (`agent/learning/outcomes.py`). Toolset: memory +
  session_search + recall_episode.
- Prompt (house style): "Review the week like sleep consolidates a day. Find
  what repeated, what you consistently got wrong, what preferences showed up
  more than once. Promote only patterns with real evidence (seen ≥ N times or
  across ≥ M days). Propose memory updates, goal changes, and automations
  through the suggestions inbox; write durable, topic-tagged semantic memories
  DIRECTLY for high-confidence repeated facts (that's your job here); update
  the narrative; note contradictions for the decay pass." Evidence threshold
  config `memory.dreaming.promote_min_occurrences` (default 3).
- Output parsed like reflection (narrative block + suggestions); activity
  source `"dreaming"` (extend the enum + note UI chip handoff if needed).
- Tests: job creation idempotency, prompt input assembly bounds, threshold
  gating (thin evidence → no promotion), parse of consolidation output.

## Loop 3 — Memory decay + contamination control (Codex)

**Goal:** Marvi currently only ADDS memory. Add forgetting and reconciliation
so the store stays sharp (MemGuard-style contamination prevention + relevance
decay).

- `agent/memory/decay.py`, run as a step INSIDE the dreaming job (no separate
  cron):
  - **Recency/usage scoring:** each semantic memory entry gets a decayed
    relevance from age + last-referenced (track a lightweight "last surfaced"
    timestamp when an entry is injected into a prompt — add to MemoryStore).
    Entries below `memory.decay.archive_threshold` and older than
    `memory.decay.min_age_days` (default 60) are ARCHIVED (moved to
    `HERMES_HOME/memory/archive/`, not deleted) — recoverable, out of the hot
    prompt.
  - **Dedup/merge:** near-duplicate entries (high text similarity, cheap —
    token overlap or the FTS score, no embeddings required) are merged; the
    merge that would drop information is proposed via suggestions, a pure
    duplicate is merged autonomously and logged.
  - **Contradiction flagging:** entries that directly conflict (dreaming pass
    or a cheap heuristic surfaces them) → a suggestion asking the user which
    is current ("You told me X in April and Y last week — which holds?").
- NEVER hard-deletes user memory; archive is the strongest autonomous action.
- Config `memory.decay.{enabled(default true), archive_threshold,
  min_age_days, dedup_similarity(default 0.85)}`.
- UI: "What Marvi knows" viewer gains an "Archived" collapsible + a restore
  button (`POST /api/memory/restore/{id}`); contradiction suggestions render in
  the inbox.
- Tests: decay scoring, archive threshold + min-age gate, dedup merge (safe vs
  info-dropping), restore, contradiction detection, never-hard-delete invariant.

## Loop 4 — Adaptive retrieval / Retrospective Reflection (Codex; lowest priority)

**Goal:** learn WHICH memories are worth injecting. Today memory is injected
statically; it never learns that some entries are noise in some contexts.

- Track per-entry usefulness: when a memory is injected AND the turn's outcome
  was good (no correction, task succeeded — reuse the escalation/outcomes
  signals from the learning round), nudge its usefulness up; injected but the
  user immediately corrected or the memory was irrelevant → nudge down. Store
  the weight in MemoryStore (sidecar `memory/usefulness.json`, entry-id keyed).
- Retrieval ranking (where MemoryStore selects entries for the prompt within
  the char budget) becomes usefulness-weighted, not just recency/order.
- This is a soft signal — bounded nudges, never removes an entry (that's decay's
  job); floors so a temporarily-unused fact can recover.
- Config `memory.retrieval.{adaptive(default true), learning_rate}`.
- Tests: usefulness update on good/bad outcome, ranking reflects weights, floor
  prevents starvation, disabled → pure recency (unchanged).

## Config summary

```yaml
memory:
  episodic: {enabled: true, retain_days: 400, min_importance_for_prompt: 0.4}
  dreaming: {schedule: "0 4 * * 0", promote_min_occurrences: 3}
  decay: {enabled: true, archive_threshold: 0.2, min_age_days: 60, dedup_similarity: 0.85}
  retrieval: {adaptive: true, learning_rate: 0.1}
```

## API summary
- GET /api/memory/episodes (Timeline tab) · recall_episode tool
- POST /api/memory/restore/{id} (decay archive restore)
- Knowledge viewer gains archived section + restore
- activity.jsonl sources: + "dreaming" (episodes are their own store, not the
  activity feed, though meaningful ones are cross-recorded)

## Build order
1. **Loop 1 episodic** (Claude's agent, NOW) — foundation the rest query.
2. **Loop 2 dreaming** — the weekly consolidation tier.
3. **Loop 3 decay** — runs inside dreaming; keeps the store from rotting.
4. **Loop 4 adaptive retrieval** — soft ranking, last, needs outcome signals.

## Non-goals (v1)
Vector embeddings anywhere (FTS5/BM25 only, consistent with the document brain);
hard-deleting user memory (archive only); a separate dreaming engine (it's a
cron job like reflection); editing episodes from the UI (read-only timeline);
retrieval that removes entries (that's decay's job, consent-gated).
