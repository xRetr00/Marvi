# Marvi Subconscious + Presence — Design Spec

Date: 2026-07-09 · Status: approved by user · Branch: `feat/marvi-subconscious-presence`

## Vision

Make Marvi (the product identity of this repo; Hermes is the base engine) a proactive
agent with a subconscious: it knows what the user is doing on their desktop, keeps a
goal store, diffs the user's world cheaply, and acts or suggests — with consent tiers —
instead of waiting for prompts. Inspired by OpenHuman's subconscious loop, but built on
existing Hermes machinery (cron, suggestions, script injection, memory, gateway) and
strictly cheaper: no LLM call unless the world actually changed.

## Approved decisions

- **Collector**: ActivityWatch (localhost:5600) is the main desktop collector. We build
  only `aw-watcher-media` (now-playing depth AW lacks), posting events into AW's store.
- **Platform**: Windows-first for the media watcher (SMTC via `winsdk`). OS adapters
  behind one event schema; macOS/Linux later.
- **Privacy**: no filter — all local AW data is fair game for LLM prompts and memory
  distillation. Keep an *empty-by-default* `presence.denylist` config hook (one `if`).
  Raw data never persists outside the machine; only distilled summaries enter memory.
- **V1 proactivity**: distillation + flow-aware delivery ON by default. "Goblin mode"
  (shoulder-tap stuck detection, zero-cold-start session priming) ships too but OPT-IN
  via settings.
- **Accounts**: Gmail/GitHub/etc. via Composio. Sync must be *smart*: delta fetches +
  local snapshot diffing; never OpenHuman-style blind polling that burns API limits.
- **Branding**: all UI/visible surfaces say **Marvi**; the agent identifies as Marvi.
- **UI**: desktop app (apps/desktop) gets settings + viewer surfaces for all of this.

## Architecture

```
SMTC/OS APIs → aw-watcher-media ─┐
AW window/afk/browser watchers ──┼→ ActivityWatch store (local)
                                 │
        desktop_context tool  ←──┤  on-demand agent tool
        presence distiller    ←──┤  nightly LLM call → memory
        flow gate / goblin    ←──┘  reads current state, LLM only on trigger

Composio delta fetchers → snapshot store (~/.hermes/subconscious/snapshots/)
                              │ diff
Subconscious tick (cron job): stage 1 pre-script (mechanical diff, exit if NO_CHANGE)
                    → stage 2 LLM pass (diff + goals + memory)
                    → [SILENT] | proactive message | suggestion (consent tiers)
Idle trigger (gateway): fires a tick after N min of user silence post-activity.
Goal store (~/.hermes/goals.json): steering input, injected into system prompt.
```

## Workstream contracts (file ownership)

To allow parallel implementation, each workstream owns its files; shared contracts are
defined here and must not be renegotiated.

### Contract 1 — snapshot script protocol
The subconscious tick cron job runs a pre-script via existing cron script injection.
The script prints to stdout either the literal line `NO_CHANGE` (tick exits, zero LLM
cost) or a human-readable diff of what changed. Script entry point:
`cron/scripts/subconscious_snapshot.py` (owned by Workstream C, invoked by A).

### Contract 2 — suggestion source
`cron/suggestions.py` gains source value `"subconscious"` plus a per-category
proactivity tier: `notify` → `propose` (one-tap accept) → `auto` (pre-approved
categories only). Owned by Workstream A; UI (D) reads/writes tiers via config.

### Contract 3 — config keys (hermes config / cli-config.yaml)
```yaml
subconscious:
  enabled: false          # `hermes subconscious enable` flips this + creates tick job
  interval: "20m"         # tick cadence; adaptive backoff when quiet
  idle_trigger_minutes: 15
  tiers: {}               # category -> notify|propose|auto
presence:
  enabled: false
  flow_gating: true
  distill_schedule: "0 3 * * *"
  denylist: []            # empty default; strip matching titles pre-LLM when set
  goblin:
    shoulder_taps: false  # opt-in
    session_priming: false
composio:
  api_key: null
  surfaces: []            # e.g. [gmail, github]
```

### Contract 4 — Marvi identity
Agent system prompt states it is **Marvi**, built on the Hermes engine by xRetro Labs.
User-visible strings in new surfaces say Marvi, never Hermes.

## Workstreams

### A. Goals + subconscious core (cron/, agent/, gateway/, hermes_cli/)
1. **Goal store**: `agent/goal_store.py` — `~/.hermes/goals.json`, atomic writes +
   locking mirroring `cron/jobs.py` storage style. Fields: id, title, detail, status
   (active/paused/done), created, updated, horizon (short/long).
2. **Goal tools**: register `goal_add`, `goal_update`, `goal_list` via
   `tools.registry` (own toolset `goals`).
3. **Prompt injection**: active goals appended to system prompt where memory manager
   builds its prompt section.
4. **Subconscious tick**: `hermes subconscious enable|disable|status` CLI. Enable
   creates a built-in cron job (existing `cron.jobs.create_job`, NO second engine)
   with pre-script per Contract 1 and a stage-2 prompt that reasons over diff + goals
   + recent memory, ending in `[SILENT]`, a delivered message, or a suggestion.
5. **Idle trigger**: gateway hook — after `idle_trigger_minutes` of silence following
   an active session, fire one tick run (reuse the same job, debounced).
6. **Suggestions**: Contract 2 in `cron/suggestions.py`; morning-briefing entry in
   `cron/suggestion_catalog.py` (goals + overnight diff + calendar).
7. **Marvi identity** per Contract 4.

### B. Presence (tools/presence/, gateway/delivery.py, hermes_cli/)
1. **AW client**: `tools/presence/aw_client.py` — thin REST client for localhost:5600
   (buckets, events, afk state). Graceful "presence unavailable" when AW is down.
2. **`desktop_context` tool**: registration shim `tools/desktop_context_tool.py`
   (mirror `computer_use_tool.py` pattern), impl in `tools/presence/`. Modes:
   `now` (foreground app, window title, parsed VS Code workspace/file, now-playing,
   idle state, session length) and `today`/`week` (aggregates).
3. **Media watcher**: `tools/presence/media_watcher.py` — Windows SMTC via `winsdk`
   (optional dep, guarded import), posts `aw-watcher-media` events to AW. Managed as
   a supervised process; `hermes presence setup|status|pause` CLI.
4. **Distiller**: presence CLI setup creates a cron job (direct `create_job`, not
   catalog) that reads AW since last run and distills durable observations into
   memory; `[SILENT]` when nothing meaningful.
5. **Flow gate**: in gateway delivery path — when a proactive/cron message is ready
   and user is active in a focus app (IDE/editor, low app switching), hold in queue;
   flush on idle/context switch. No-op when AW unavailable or `flow_gating: false`.
6. **Goblin (opt-in)**: session priming (inject last-hour presence summary into new
   session context) and shoulder-tap heuristic (same file >45 min + error-ish titles
   + rapid search-tab switching → spawn background investigation, message findings).

### C. Composio smart sync (cron/scripts/subconscious/, tools/)
1. **Snapshot store**: `cron/scripts/subconscious/snapshot_store.py` —
   `~/.hermes/subconscious/snapshots/`, per-surface JSON snapshots, atomic writes.
2. **Delta fetchers**: per-surface modules using Composio's Python API with real
   delta primitives — Gmail history API cursor, GitHub notifications `since`, etc.
   Never full refetches; respect rate limits; exponential backoff.
3. **Entry script**: `cron/scripts/subconscious_snapshot.py` per Contract 1 —
   iterates configured surfaces, fetches deltas, diffs vs snapshots, prints
   `NO_CHANGE` or the diff summary.
4. **CLI**: `hermes composio connect <app>` / `list` — stores config, verifies auth.

### D. Desktop app UI (apps/desktop/)
Marvi-branded settings + viewers, following existing app patterns and whatever
config/API plumbing exists (add minimal gateway endpoints only if none fit):
1. **Subconscious settings**: enable toggle, interval, idle trigger, tier matrix.
2. **Presence settings**: enable, flow gating, goblin opt-ins, denylist editor,
   AW status indicator.
3. **Goals panel**: list/add/edit/complete goals.
4. **"What Marvi knows"**: viewer over distilled presence/subconscious memories.
5. **Connected accounts**: Composio surfaces list + connect status.

## Error handling
AW down → tool degrades with clear message; watcher crash → supervisor restart;
distiller/tick with no data → `[SILENT]`, no LLM call; Composio auth failure →
surface marked broken in status, never crash the tick.

## Testing
Unit: title parsing, goal store CRUD, snapshot diffing, tier logic, stuck heuristic
(fixture event streams, assert no false-positive taps). Integration: fake AW server
→ `desktop_context(now)`; fake snapshots → tick script prints NO_CHANGE/diff.
