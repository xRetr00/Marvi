# Marvi Deep Subconscious + Document Brain + Memory Tree — Implementation Spec

Date: 2026-07-14 · Status: approved by user · Implementer: Codex agent · Branch: main

## Vision

Elevate Marvi from a monitoring daemon to a human-like presence on the PC. Today
the subconscious tick wakes every 20 minutes with amnesia, reacts to an inbox
diff, and forgets it ever thought anything. This spec adds: a continuous inner
narrative (the subconscious remembers its own thoughts), a nightly reflection
that thinks about the USER rather than the inbox, bounded self-directed
initiatives, goal inference from observed behavior, a local document brain
(index of the user's own files), a memory tree view, and UI for all of it.

## Ground rules for the implementer (read first)

- Product name is **Marvi** in every user-visible string; code identifiers may
  say hermes. Agent identity/persona comes from SOUL.md via
  `agent/prompt_builder.py::load_soul_md` — never hardcode a persona.
- Config: read with the `cfg_get(cfg, "a", "b", default=...)` pattern (see
  `cron/subconscious.py`); do NOT add keys to DEFAULT_CONFIG unless a key is
  user-editable from the desktop settings (then mirror how existing
  subconscious/presence keys are exposed).
- Web endpoints: FastAPI in `hermes_cli/web_server.py`; sync file/HTTP work in
  endpoints goes through `run_in_threadpool` (mirror the existing
  `/api/subconscious/*` section added 2026-07-14). Never block the event loop —
  there is a `[LOOP-LAG]` watchdog (`gateway/loop_watchdog.py`) that will name
  you in the logs if you do.
- Background thinking is logged to the append-only
  `HERMES_HOME/subconscious/activity.jsonl` (capped 500 lines, rotation
  implemented in `cron/scheduler.py`; entry fields: at, source, outcome,
  summary, diff ≤4000ch, thought ≤4000ch, output_path). Extend `source` with
  the new values introduced below; the desktop Activity timeline
  (`apps/desktop/src/app/settings/subconscious/activity-*.tsx`) renders it.
- Consent-first is inviolable: nothing outward-facing happens without flowing
  through the suggestions store (`cron/suggestions.py`, tiers
  notify/propose/auto per category in config `subconscious.tiers`) or normal
  delivery. New autonomous behaviors must be budgeted and configurable.
- Prompt house style: plain direct prose, second person, explicit rules,
  literal contracts spelled out (see the goblin investigation prompt in
  `tools/presence/goblin.py` and `DISTILL_SYSTEM_NOTE` in
  `tools/presence/distill.py`). The `[SILENT]` convention means "produce
  exactly [SILENT] and nothing else to stay quiet".
- The tick is ONE cron job (`cron/subconscious.py::enable` creates it; job
  runs a stage-1 pre-script `cron/scripts/subconscious_snapshot.py` whose
  literal `NO_CHANGE` last line skips the LLM via `_parse_wake_gate` in
  `cron/scheduler.py`). Do not build a second engine — new periodic behaviors
  are additional cron jobs created idempotently by `enable()`.
- Runtime note: the user's live system runs from the INSTALLED copy at
  `%LOCALAPPDATA%\hermes\hermes-agent` with `HERMES_HOME=%LOCALAPPDATA%\hermes`;
  this repo is the dev tree. Ship in the repo; the user syncs the install.
- Do not touch voice/duplex files (`tools/voice_instant_lane.py`,
  `tools/voice_speaker_id.py`, voice sections of web_server, apps/desktop
  voice-island/chat voice) except the single whitelist addition named in §5.
- Tests are required per feature, following the adjacent suite's conventions
  (`tests/cron/`, `tests/hermes_cli/test_web_server_marvi.py`,
  `tests/tools/`, desktop vitest colocated). Known pre-existing failures:
  Windows chmod tests in tests/cron, jsdom "document is not defined" in some
  desktop settings suites — baseline with git stash before claiming breakage.

---

## 1. Inner narrative — the subconscious's working memory

**File:** `HERMES_HOME/subconscious/narrative.md`. Cap ~8000 chars. Atomic
writes (tempfile + replace, 0600 — mirror `cron/suggestions.py` storage style).
Keep the 3 previous versions as `narrative.md.1..3` rotation.

**Contract:** the stage-2 tick prompt (in `cron/subconscious.py`, the prompt
assembled at `enable()`/tick time) gains two parts:
1. The current narrative injected under a header: "Your working notes from
   previous background runs (your own thoughts — continue them):".
2. An output contract appended to the prompt: after its normal output (or
   `[SILENT]`), the agent MUST emit an updated narrative between literal
   markers `<narrative>` and `</narrative>` — carrying forward open questions,
   dropping resolved ones, adding new observations. If it emits no block, the
   narrative is left unchanged.

**Parsing:** in `cron/scheduler.py` where the agent stage completes for the
subconscious tick job (same hook that writes activity.jsonl), extract the
block, strip it from any DELIVERED output (the user never sees raw markers),
persist it, and store the narrative diff summary in the activity entry
(field `narrative_updated: true/false`). The extraction helper lives in
`cron/subconscious.py` (`extract_narrative_block(text) -> tuple[clean_text,
narrative_or_none]`) with unit tests: block present/absent/malformed/multiple
(last wins)/oversize (truncate at cap, log once).

**Consumers:** tick stage-2 prompt, reflection prompt (§2), goblin
investigation prompt context (one-line summary only), morning briefing.

**Cold start:** absent file → inject nothing, contract still applies (first
run writes the first narrative).

## 2. Nightly reflection — thinking about the user

**Job:** a second built-in cron job, name `Subconscious reflection`, default
schedule `30 3 * * *` (after the presence distiller at 03:00), created
idempotently by `cron/subconscious.py::enable()` (re-running enable on an
existing install must add it without duplicating; disable removes/pauses both
jobs). Schedule via config `subconscious.reflection.schedule`.

**Inputs assembled into its prompt** (build in `cron/subconscious.py`, keep
each bounded): current narrative; last 24h of activity.jsonl (summaries only);
today's presence digest (reuse `tools/presence/distill.py::build_digest`
data or the last distiller output); active goals
(`agent/goal_store.py::format_active_goals_for_prompt`); pending suggestions;
rhythm summary (`tools/presence/rhythm.py::get_rhythm`). Toolsets: same as the
tick plus `session_search` (it may search past conversations).

**Its job, stated in the prompt (house style):** review the day; update the
model of the user; carry the narrative forward; decide follow-ups on its own
past suggestions/initiatives (did the user act? drop or retry politely);
propose tomorrow's initiatives (§3) with concrete trigger conditions; propose
goal changes (§4); optionally compose a morning briefing. Output contract:
optional prose (delivered per `subconscious.reflection.deliver` config,
default the same delivery target as the tick; `[SILENT]` allowed), then
`<narrative>` block, then optional `<initiatives>` block (§3), each parsed by
the same scheduler hook. Activity entry source: `"reflection"`.

## 3. Initiatives — bounded self-directed action

**Store:** `HERMES_HOME/subconscious/initiatives.json` — list of
`{id, intent (one imperative sentence + context), trigger, created_by
("reflection"|"tick"), created_at, expires_at (default +48h), status
("pending"|"done"|"expired"|"cancelled")}`.
Trigger is `{type: "next_tick"} | {type: "time", at: ISO} | {type: "rhythm",
window: "deep_work_start"|"active_start"|"active_end"} | {type: "presence",
condition: "idle"|"coding"|"not_heavy"}`. Atomic writes, module
`cron/subconscious_initiatives.py` with pure `is_due(initiative, now, rhythm,
presence_state) -> bool` (unit-testable, presence/rhythm callables injected).

**Creation:** reflection (and optionally a tick) emits an `<initiatives>`
fenced JSON array; the scheduler hook validates (schema + max 5 per run) and
appends. Malformed → log once, ignore.

**Execution:** stage-1 (`cron/scripts/subconscious_snapshot.py`) additionally
evaluates due initiatives (time/rhythm mechanically; presence via a guarded AW
probe with ≤1.5s timeout, failure = not due). Due initiatives are printed as a
`## initiatives` diff section — which naturally defeats the NO_CHANGE gate, so
stage-2 wakes and the prompt instructs: execute due initiatives within normal
consent rules (deliver a message, create a suggestion, or [SILENT] with a
narrative note if the moment is wrong), then mark disposition in a literal
`<initiative_results>` JSON block (id → done|skip|retry_later) parsed by the
scheduler hook to update the store.

**Budget:** config `subconscious.initiative_budget_per_day` (default 3).
Enforced mechanically in stage-1: initiatives beyond the daily executed count
are not marked due (counter persisted in initiatives.json, reset by date).
Budget exhausted → they wait for tomorrow.

## 4. Goal inference from behavior + weekly review

**Inference (in the reflection prompt):** using presence digests, activity,
session history, and the narrative, when the evidence shows a sustained
pursuit not covered by an active goal, propose it — via the existing
suggestions store, NOT by creating goals directly.

**Store change (`cron/suggestions.py`):** suggestions currently carry a cron
`job_spec`. Add a discriminated `kind: "job" | "goal"` (default "job",
backward compatible) with `goal_spec: {title, detail, horizon}` for kind
"goal". `accept_suggestion` for kind "goal" calls
`agent/goal_store.py::add_goal` instead of `create_job`. Category `"goal"`,
source `"subconscious"`, tier semantics unchanged (goal suggestions are always
propose-tier; never auto). Dedup key from a slug of the title so a dismissed
goal idea isn't re-proposed (existing latch mechanism).

**Weekly review:** the reflection prompt, on the configured weekday
(`subconscious.reflection.review_weekday`, default Sunday), also reviews
active goals against the week's evidence: stale (no evidence 3+ weeks) →
propose pause; evidently completed → propose done. These are goal-kind
suggestions too (`goal_spec.action: "pause"|"done"|"add"` — extend goal_spec
with an action field, default "add").

**Existing tool:** `goal_add`/`goal_update`/`goal_list` in
`tools/goal_tools.py` remain the chat-side interface; the reflection path is
suggestions-only so the user always taps accept.

## 5. Document brain — Marvi can recall the user's own files

**Scope v1 (deliberate):** local full-text index, FTS5/BM25 only. NO vector
database, NO embeddings, NO new heavy deps — the repo already uses SQLite FTS5
for session search (find that implementation and mirror its schema/query
style). Embeddings are a flagged v2 (`# ponytail:` comment at the seam).

**Module:** `tools/brain/` package — `indexer.py`, `store.py` (SQLite at
`HERMES_HOME/brain/index.db`: files table (path, mtime, size, hash, status) +
chunks FTS5 table (file_id, ord, text)), `extract.py` thin wrapper over the
existing `tools/read_extract.py` extraction (pdf/docx/txt/md/code). Chunking:
~1200 chars with 150 overlap, plain function, tested.

**Config:** `brain.enabled` (default false until folders configured),
`brain.folders: []` (absolute paths), `brain.exclude: []` (glob substrings;
sensible built-ins: node_modules, .git, venv, >20MB files, binaries),
`brain.schedule` (default `0 */6 * * *`).

**Indexing job:** cron job `Brain indexer` created by `hermes brain enable`
(CLI `hermes_cli/brain_cmd.py`: `enable|disable|status|index|search`,
self-registering parser mirroring `presence_cmd.py`). Incremental: unchanged
mtime+size → skip; deleted → purge. `hermes brain index` runs one pass
synchronously. The job needs no LLM: run it as a script-only job (stage-1
script does the indexing and prints NO_CHANGE — zero LLM cost) exactly like
the snapshot pattern.

**Recall tool:** `recall_files(query, k=6)` registered via `tools.registry`
(toolset `memory` or a new `brain` toolset included in core), returning path +
snippet + score. Add to the instant lane's read-only whitelist
(`tools/voice_instant_lane.py` — the ONE permitted voice-file touch) and to
the subconscious/reflection toolsets. Answering "what did that contract say"
must work from chat, voice, and background thinking alike.

**Endpoints:** `GET /api/brain/status` (folders, file/chunk counts, last run,
errors), `GET /api/brain/search?q=` (same as the tool), `PUT
/api/brain/folders` (update config; validate paths exist).

## 6. Memory tree — structure over the flat memory

**Scope v1 (deliberate — a VIEW, not a store rewrite):** the flat
`§`-delimited `HERMES_HOME/memories/USER.md` + `MEMORY.md`
(`tools/memory_tool.py::MemoryStore`) stay the source of truth. Add topic
paths on top:

- **Convention:** a memory entry MAY begin with a bracketed topic path:
  `[projects/marvi] Voice latency was the main v1 blocker...`. Parsing helper
  in `tools/memory_tool.py` (`split_topic(entry) -> (topic|None, text)`), used
  everywhere entries are rendered; writing tools accept an optional topic.
- **Writers:** update the memory tool schema/description so agents CAN pass a
  topic; update the distiller (`DISTILL_SYSTEM_NOTE`) and reflection prompts
  to instruct topic-tagged saves using a small stable taxonomy the prompt
  suggests (projects/<name>, people/<name>, life/work, life/study,
  preferences, systems/marvi) but free-form is allowed.
- **Migration:** `hermes memory organize` CLI — one-shot LLM pass (auxiliary
  model, batched) proposing topics for existing untagged entries; writes only
  on `--apply`. Idempotent, safe on 0 entries.
- **API:** extend `GET /api/marvi/knowledge` entries with `topic` (nullable);
  add `?tree=1` returning `{topic_path: [entries]}` grouped.
- **v2 flag:** true hierarchical store (per-topic files, links) is explicitly
  out of scope; leave a pointer comment.

## 7. UI (apps/desktop — all inside the existing "Presence" settings hub)

Follow existing patterns: Radix Tabs in `settings/presence/index.tsx`,
primitives in `settings/primitives.tsx`, transport hooks like
`settings/subconscious/use-marvi-config.ts` / `activation-service.ts`,
timeline components in `settings/subconscious/activity-*.tsx`.

1. **Mind section** (top of the Subconscious tab, above Activity):
   - Narrative viewer: rendered markdown of narrative.md (new
     `GET /api/subconscious/narrative`, plus `?history=1` returning the
     rotated versions); a subtle "last updated Xm ago by tick/reflection"
     line. Read-only v1.
   - Initiatives list: pending/done with trigger description, expiry, and a
     cancel button (`POST /api/subconscious/initiatives/{id}/cancel`); daily
     budget indicator ("2 of 3 used today").
   - Reflection runs already appear in the Activity timeline via source
     `"reflection"` — add its chip/icon + filter.
2. **Goals panel:** inferred-goal suggestions render inline at the top of the
   existing goals panel (they arrive through the suggestions endpoints with
   category "goal") with an evidence line (suggestion summary) and
   Accept/Dismiss. Weekly-review pause/done proposals render on the affected
   goal card.
3. **Brain tab** (new tab in the Presence hub): watched-folders editor
   (add/remove, existence validation), exclude list, index stats
   (files/chunks/last run/errors), a search box hitting /api/brain/search
   with result snippets, and an enable flow that calls `hermes brain
   enable`-equivalent endpoint (`POST /api/brain/enable|disable` wrapping the
   CLI functions — refactor them importable like presence_cmd's setup was).
4. **Knowledge viewer → tree:** upgrade "What Marvi knows" to group by topic
   (collapsible tree from the `?tree=1` API; untagged under "unsorted"),
   with the existing flat list as fallback and a client-side text filter.

Empty states must explain themselves (mirror the Activity section's copy
style: "Reflection runs nightly at 03:30 — first run tonight").

## 8. Config summary (all cfg_get unless noted)

```yaml
subconscious:
  reflection:
    schedule: "30 3 * * *"
    deliver: null            # default: tick's delivery target
    review_weekday: 6        # Sunday
  initiative_budget_per_day: 3
brain:
  enabled: false
  folders: []
  exclude: []
  schedule: "0 */6 * * *"
```

## 9. New/changed API summary

- GET /api/subconscious/narrative (+ ?history=1)
- GET /api/subconscious/initiatives · POST /api/subconscious/initiatives/{id}/cancel
- GET /api/brain/status · GET /api/brain/search?q= · PUT /api/brain/folders ·
  POST /api/brain/enable · POST /api/brain/disable
- GET /api/marvi/knowledge: + topic field, + ?tree=1
- activity.jsonl source values: + "reflection" (and initiatives executions are
  ordinary tick entries whose summary names the initiative)
- suggestions store: + kind ("job"|"goal"), + goal_spec {title, detail,
  horizon, action: add|pause|done}

## 10. Build order (each step ships + tests independently)

1. Narrative (module + parser + tick prompt + endpoint + Mind viewer) — makes
   every existing tick smarter immediately.
2. Reflection job (prompt + parsing + activity source + chip).
3. Initiatives (store + stage-1 due-eval + execution contract + budget + UI).
4. Suggestions `kind:"goal"` + goal inference + weekly review + goals-panel UI.
5. Document brain (store/indexer/tool/CLI/endpoints/Brain tab).
6. Memory topics (helper + writer prompts + organize CLI + tree API/UI).

## 11. Non-goals (v1)

Vector embeddings; hierarchical memory store rewrite; editing the narrative
from the UI; initiatives that bypass consent tiers; indexing cloud drives;
any change to voice/duplex behavior beyond the single recall_files whitelist
addition.
