# Marvi Learning Loops — Implementation Spec

Date: 2026-07-16 · Implementer: Codex · Branch: main · Status: approved

## Vision

Marvi already senses (email/calendar/slack deltas, desktop presence, rhythm,
the physical room) and acts (subconscious tick, reflection, suggestions,
world triggers). This spec adds the third leg: **learning** — Marvi adapts to
the user from its own observed outcomes, the way voice speaker-ID now learns
the owner's voice with an adaptive embedding ring. Six loops, all riding
existing infrastructure. The proactivity mandate governs: every loop feeds the
suggestions inbox / tick / reflection — none acts autonomously without consent.

## Ground rules (Codex read first)

- Product name **Marvi** in user-visible strings; code identifiers may say hermes.
- Config via `cfg_get(cfg, "a", "b", default=...)` (see `cron/subconscious.py`);
  add to DEFAULT_CONFIG only for keys the desktop UI edits.
- Endpoints in `hermes_cli/web_server.py`; sync file/HTTP work wrapped in
  `run_in_threadpool` (the `[LOOP-LAG]` watchdog in `gateway/loop_watchdog.py`
  will flag event-loop blocking).
- Consent-first is inviolable. Every learned proposal flows through
  `cron/suggestions.py` (kinds "job"|"goal"; add "config" below) with tiers
  notify/propose/auto per category — learned proposals are ALWAYS propose-tier,
  never auto. Dismissed proposals latch by dedup_key.
- Storage style: atomic tempfile+replace, 0600, in-process lock — mirror
  `cron/suggestions.py`. Learning stores live under
  `HERMES_HOME/learning/<loop>.json`.
- Background thinking logs to `HERMES_HOME/subconscious/activity.jsonl` (source
  enum incl. tick/idle_trigger/distiller/goblin/world/reflection); learning
  proposals surface as suggestions, and the reflection that generates them is
  already a "reflection" activity entry.
- The reflection job (`cron/subconscious.py`, "Subconscious reflection",
  ~03:30) is the natural home for the weekly/analytical loops — extend its
  prompt inputs and output parsing rather than adding new cron jobs where a
  reflection clause suffices.
- Prompt house style: plain prose, second person, explicit rules, literal
  contracts (see `tools/presence/goblin.py`, `DISTILL_SYSTEM_NOTE`).
- Every loop is unit-tested (fakes only); learned proposals must be
  reproducible from a fixture ledger. Known pre-existing failures: Windows
  chmod tests in tests/cron, jsdom "document is not defined" in some desktop
  suites — baseline with git stash before claiming breakage.
- Installed-copy note: the user's live system runs from
  `%LOCALAPPDATA%\hermes\hermes-agent`; ship in this repo.
- A parallel Codex stream may hold uncommitted WIP in plugins/smart_room/** and
  some apps/desktop voice files — re-check git status before finishing; never
  revert others' work.

## Shared substrate — build this first (Loop 0)

### 0.1 Suggestion kind "config"
`cron/suggestions.py` currently supports kind "job" and "goal" (goal added
2026-07-14 with `goal_spec`). Add kind **"config"** with `config_spec:
{path: "dotted.key.path", value: <new>, current: <old>, rationale: str,
scope: "user"}`. `accept_suggestion` for kind "config" writes the key via the
same config save path the settings API uses (validate the dotted path exists
or is a known learnable key from the registry in 0.3). Category = the loop's
category (below). Always propose-tier. Dedup key = slug(path + rationale-topic)
so a declined tuning isn't re-proposed with trivial wording changes.

### 0.2 Outcomes ledger (the core new store)
`agent/learning/outcomes.py` — append-only, capped, per-category event log at
`HERMES_HOME/learning/outcomes.jsonl` (rotate at 2000 lines). One entry:
```json
{"at": ISO, "loop": "trust|voice_threshold|focus_apps|escalation|timing|room_habit",
 "category": "<suggestion category or signal category>",
 "event": "accepted|dismissed|corrected|ignored|observed",
 "ref": "<suggestion_id or signal id>", "detail": {...}}
```
Public API: `record(loop, category, event, ref=None, detail=None)`,
`recent(loop=None, category=None, since=None, limit=500) -> list`,
`counts(loop, category, window_days) -> {accepted, dismissed, ...}`. Thread-safe,
never raises out to callers (log+swallow). This is the memory every loop reads.

### 0.3 Learnable-config registry
`agent/learning/registry.py` — a static dict of keys the config loop is allowed
to propose changes to, each with `{type, min, max, step, category, human}`.
Seed it with the keys the loops below tune (voice thresholds, focus apps list,
timing windows). accept_suggestion(kind=config) MUST reject any path not in
this registry (safety: learning can never rewrite arbitrary config). Unit-test
the guard.

### 0.4 Suggestion outcome hook
Wherever `accept_suggestion`/`dismiss_suggestion` run (cron/suggestions.py),
call `outcomes.record(loop=suggestion.loop or "trust", category, event)`.
Add an optional `loop` field to suggestion records (default "trust") so the
ledger knows which loop a suggestion came from. This closes the loop: a
proposal's fate becomes training data for the next proposal.

## Loop 1 — Trust ledger (autonomy that's earned)

**Goal:** after the user consistently accepts a category of suggestion, Marvi
proposes promoting that category's tier (propose → auto), and conversely
proposes demotion/backing-off when a category is consistently dismissed.

- Reads `outcomes.counts(category, window_days=30)`.
- Promotion rule (in the reflection prompt clause + a pure helper
  `agent/learning/trust.py::evaluate_trust(counts, current_tier) ->
  Optional[proposal]`): category at propose-tier with >= `trust.promote_after`
  (default 8) accepts and 0 dismisses in the last 10 outcomes → emit a
  kind="config" suggestion setting `subconscious.tiers.<category>` = "auto",
  rationale naming the streak ("You've accepted every calendar-conflict fix —
  want me to handle these automatically?").
- Demotion rule: category at auto/propose with >= `trust.demote_after`
  (default 3) recent dismissals → propose dropping it to notify.
- Category "goal" and any security-adjacent category are EXCLUDED from
  auto-promotion (config `trust.never_auto: [...]`, seed with goal).
- Surfaced in the reflection run (weekly is enough; gate with
  `trust.review_weekday` default Sunday, reuse the reflection's existing
  weekday plumbing).
- Config: `learning.trust.{promote_after, demote_after, never_auto,
  review_weekday, enabled(default true)}`.
- Tests: promotion fires at threshold, blocked by any dismissal, excluded
  categories never promote, demotion path, dedup (declined promotion not
  re-offered for 30 days).

## Loop 2 — Room habit learning

**Goal:** learn the user's manual room routines and propose automations for
them. Depends on the now-wired smart_room subconscious surface (transitions
flow into the tick/reflection as of 2026-07-16).

- Store `HERMES_HOME/learning/room_habits.json`: rolling histogram of
  (weekday, hour-bucket, action) from smart_room transition events where
  `source=manual` (user set a mode/scene/light by hand, not automation).
  Fed by a small consumer in the reflection input assembly that reads recent
  smart_room activity entries — NO change to the plugin (read-only).
- `agent/learning/room_habits.py::propose_automations(histogram) ->
  list[proposal]`: an action recurring >= `room.habit_min_occurrences`
  (default 4) at a consistent (weekday,hour) with low variance → a kind="job"
  suggestion creating the automation (a Marvi cron job calling
  `smart_room_set_mode`/`smart_room_set_light` at that time), rationale citing
  the observed pattern ("You've set Focus mode ~15:00 on 5 of the last 7
  weekdays — automate it?"). Also detect consistent CANCELLATIONS ("you cancel
  evening-sleep every Friday") → propose a schedule exception.
- Config: `learning.room.{habit_min_occurrences, enabled(default true)}`.
- Tests: histogram accumulation, occurrence/variance thresholds, manual-vs-
  automation source filtering (automation-driven changes never count as
  habits), cancellation-pattern detection, dedup.

## Loop 3 — Self-tuning voice thresholds

**Goal:** tune speaker-ID thresholds from the `[VOICE-ID]` telemetry the
2026-07-15 round added, instead of hand-guessed defaults.

- Parser `agent/learning/voice_tuning.py::analyze(voice_id_lines) -> stats`:
  read the last N days of `[VOICE-ID]` lines from the log (find the log path
  helper voice code uses; it appends to agent.log / a voice log). Compute:
  owner-score distribution (accepted-owner turns), the abstain-zone rate,
  competing_drop count, and how many owner turns landed in abstain (missed).
- `propose_threshold(stats, current) -> Optional[config proposal]`: if owner
  scores cluster well above `reject_threshold` and abstains are frequent,
  propose lowering the gap (raise reject_threshold or lower owner threshold)
  with the evidence line ("Your owner scores cluster 0.48–0.62; raising
  reject_threshold to 0.30 would have caught N strangers, cost 0 of your
  turns"). Only propose changes within the registry min/max (0.3).
- Runs in the WEEKLY reflection (needs a week of data; gate
  `voice_tuning.review_weekday`).
- Keys tuned (must be in the 0.3 registry): `voice.speaker_id.threshold`,
  `voice.speaker_id.reject_threshold`.
- Config: `learning.voice_tuning.{enabled(default true, but no proposal until
  >= min_samples telemetry lines, default 200), review_weekday}`.
- Tests: parse fixture VOICE-ID lines, proposal math, min-samples gate (no
  proposal on thin data), registry bounds clamp, dedup.

## Loop 4 — Learned focus apps

**Goal:** derive the user's real deep-work apps from ActivityWatch instead of
the static `presence.heavy_apps` list, so the flow gate stops interrupting in
apps the user forgot to list.

- `agent/learning/focus_apps.py::derive(aw_events, existing_list) ->
  Optional[proposal]`: reuse `tools/presence/rhythm.py`'s AW access pattern —
  over the last 14 days, find apps that dominate long uninterrupted foreground
  stretches (>= `focus.min_session_minutes`, default 25) but are NOT in
  `presence.heavy_apps`. Propose adding them (kind="config", appending to the
  list — config_spec value is the full new list, current is the old).
- Runs in reflection (has AW data anyway via the distiller/rhythm path).
- Config: `learning.focus_apps.{min_session_minutes, min_occurrences
  (default 5), enabled(default true)}`. Key `presence.heavy_apps` in registry.
- Tests: dominant-app detection from fake AW streams, exclusion of already-
  listed apps, occurrence threshold, dedup (declined app not re-proposed).

## Loop 5 — Escalation learning (voice)

**Goal:** learn the user's complexity boundary — when they re-ask, correct, or
express dissatisfaction right after an instant-lane answer, that turn should
have escalated.

- Signal capture in the duplex session (`hermes_cli/web_server.py` voice
  section): when an instant (non-escalated) turn is followed within
  `escalation.followup_window_seconds` (default 20) by a user utterance that
  looks like a re-ask/correction (heuristic: high text overlap with prior
  utterance, or leading correction markers "no,", "I meant", "actually",
  "that's not"), record `outcomes.record("escalation", category, "corrected",
  detail={utterance, prior_reply_len})`. Pure `is_correction(prev_utt, prev_reply,
  new_utt) -> bool` helper, unit-tested. NON-BLOCKING, best-effort, never
  affects the live turn.
- `agent/learning/escalation.py::mine_patterns(corrected_events) ->
  Optional[few-shot block]`: cluster the ask-shapes that repeatedly needed
  escalation; produce a SHORT (<= 5 example, <= 600 char) addendum appended to
  the instant lane's escalation-contract prompt ("Asks like these have needed
  the deep lane before: ..."). Stored at
  `HERMES_HOME/learning/escalation_hints.txt`, loaded by
  `tools/voice_instant_lane.py`'s prompt build (guarded, capped) — this is the
  ONE loop that adapts a prompt directly rather than proposing to the user,
  because it's low-risk (worst case: an extra escalation) and self-correcting.
  Regenerated in the weekly reflection.
- Config: `learning.escalation.{followup_window_seconds, enabled(default true),
  max_examples}`.
- Tests: is_correction heuristic (positive/negative cases, overlap threshold),
  hint-block generation + cap, prompt-append guarded load, no-signal → no file.

## Loop 6 — Proactive timing (speculative, ship gated OFF)

**Goal:** learn which delivery windows the user actually engages with vs.
ignores. Honest caveat documented in code: the engagement signal is weak
(cross-platform read/act is unreliable), so this loop DEFAULTS OFF and only
proposes, never auto-adjusts.

- Signal: when a proactive message is delivered, record delivery time; when the
  user responds within `timing.engagement_window` (default 1h), record
  "engaged" else "ignored" (best-effort; only counts platforms where a
  response is detectable — reuse whatever the gateway already knows about
  inbound-after-delivery; if nothing clean exists, record only delivery-time
  and mark this loop's data "delivery-only" and skip proposals — do NOT invent
  a cross-platform read-receipt system).
- `agent/learning/timing.py::propose_windows(engagement_by_hour) ->
  Optional[proposal]`: if engagement strongly concentrates in certain hours,
  propose a `smart_room`/rhythm-aware quiet-window config. Only with
  >= `timing.min_samples` (default 100) engagement events.
- Config: `learning.timing.enabled` default **false**.
- Tests: engagement bucketing, min-samples gate, delivery-only fallback (no
  proposal), disabled-by-default (no-op).

## UI (apps/desktop — Mind page + Presence hub)

Follow existing patterns (Mind page `apps/desktop/src/app/mind/`, settings
`settings/subconscious/`, `settings/presence/`, transport
`use-marvi-config.ts`/`activation-service.ts`, primitives).

1. **Learning panel (new section on the Mind page):** "What Marvi is learning"
   — reads a new `GET /api/learning/summary` (per-loop: enabled, samples
   collected, last proposal, pending proposals count). Each loop is a row with
   an enable toggle (writes `learning.<loop>.enabled`) and a one-line plain
   description. Empty states: "Collecting data — first tuning proposal after a
   week of use."
2. **Learned proposals in the suggestions inbox:** kind="config" and the new
   room/focus proposals render in the EXISTING suggestions inbox
   (`settings/subconscious/`) with an evidence line (the rationale) and
   Accept/Dismiss — they already flow through the suggestions endpoints, so
   this is mostly making the inbox render `config_spec`/`config` category
   nicely (show "Change <human key> from X to Y" + rationale).
3. **Trust tiers view:** the existing tier matrix (`tier-matrix.tsx`) gets a
   small "learned" badge on categories whose tier was set by an accepted trust
   proposal (track provenance in the tier config or a sidecar), so the user
   sees which autonomy Marvi earned vs. they set.
4. **Endpoints:** `GET /api/learning/summary`, `GET /api/learning/outcomes?loop=`
   (recent ledger for transparency/debugging), plus the loops' proposals ride
   the existing `/api/subconscious/suggestions` accept/dismiss.

## Config summary (all cfg_get; UI-edited ones also in DEFAULT_CONFIG)

```yaml
learning:
  trust: {enabled: true, promote_after: 8, demote_after: 3, review_weekday: 6, never_auto: [goal]}
  room: {enabled: true, habit_min_occurrences: 4}
  voice_tuning: {enabled: true, review_weekday: 6, min_samples: 200}
  focus_apps: {enabled: true, min_session_minutes: 25, min_occurrences: 5}
  escalation: {enabled: true, followup_window_seconds: 20, max_examples: 5}
  timing: {enabled: false, engagement_window_minutes: 60, min_samples: 100}
```

## New API summary
- POST accept for suggestion kind "config" (config_spec write, registry-guarded)
- GET /api/learning/summary · GET /api/learning/outcomes?loop=
- activity.jsonl: learning proposals appear as their originating reflection/tick
  entries; no new source value needed.

## Build order (each ships + tests independently)
1. **Loop 0** substrate: outcomes ledger, config-kind suggestions, learnable
   registry, accept/dismiss outcome hook. Everything else depends on it.
2. **Loop 1 trust** — highest payoff, pure ledger math, no new sensors.
3. **Loop 2 room habits** — rides the just-wired smart_room surface.
4. **Loop 4 focus apps** — rides rhythm/AW access.
5. **Loop 3 voice tuning** — needs a week of [VOICE-ID] data to be useful.
6. **Loop 5 escalation** — voice-section signal capture + prompt hint.
7. **Loop 6 timing** — gated off; ship last, minimal.
8. **UI** — learning panel + inbox rendering + trust badges, after ≥ Loop 1–2
   exist to populate it.

## Non-goals (v1)
Auto-promotion without an accepted trust proposal; learning that rewrites
config outside the registry; cross-platform read-receipt infrastructure for
Loop 6; editing learned prompts from the UI; any loop acting without the
suggestions-inbox consent step (except Loop 5's low-risk prompt hint, which is
explicitly justified above).
