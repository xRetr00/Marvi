# Marvi Autonomy + Graph Mind — Implementation Spec

Date: 2026-07-20 · Implementer: Codex · Branch: main · Status: approved

## Vision

Two architecture pieces that push Marvi from "reactive assistant" to
"companion with an inner life you can see":

1. **Autonomy / exploration freedom** — Marvi acts on its own between prompts:
   web research to answer its own open questions, visiting the user's
   university student system, and *asking the user* when a question is worth
   interrupting for. Bounded, budgeted, consent-tiered.
2. **The graph mind** — replace the flat `§`-delimited memory + separate
   stores with (or layer over them) a real knowledge graph: nodes (people,
   projects, facts, events, preferences) with typed edges (works_on,
   related_to, contradicts, caused_by, prefers), viewable and navigable in the
   UI as an actual mind map, not a list.

Governed by the proactivity mandate. The graph is the substrate; autonomy is
what walks it and grows it.

## Ground rules (Codex read first)

- **Marvi** in user-visible strings; code identifiers may say hermes.
- `cfg_get(cfg, ...)`; DEFAULT_CONFIG only for UI-edited keys.
- Endpoints in `hermes_cli/web_server.py`, sync work via `run_in_threadpool`
  (`[LOOP-LAG]` watchdog flags blocking).
- Storage: atomic tempfile+replace, 0600, in-process lock (mirror
  `cron/suggestions.py`). New stores under `HERMES_HOME/memory/`.
- Consent-first: outward or irreversible actions flow through
  `cron/suggestions.py` OR the ask-user channel below; autonomous internal
  work (graph building, research) runs freely but is logged to
  `activity.jsonl` and budgeted.
- Prompt house style: plain prose, explicit contracts (see
  `tools/presence/goblin.py`, `DISTILL_SYSTEM_NOTE`).
- Existing substrate to build ON, not replace blindly: semantic memory
  (`tools/memory_tool.py` MemoryStore, `§` entries, topic paths via
  split_topic); episodic (`agent/memory/episodic.py` SQLite/FTS5); decay
  (`agent/memory/decay.py`); adaptive retrieval (`agent/memory/retrieval.py`);
  the subconscious tick/reflection/dreaming (`cron/subconscious.py`); the
  suggestions inbox; the initiatives store
  (`cron/subconscious_initiatives.py` — trigger types time/rhythm/presence/
  next_tick); world triggers (`gateway/world_trigger.py`); the Mind page
  (`apps/desktop/src/app/mind/`).
- Tests per feature (fakes only). Known pre-existing failures: Windows chmod
  in tests/cron, jsdom "document is not defined" in some desktop suites —
  baseline with git stash before claiming breakage.
- Installed copy runs from `%LOCALAPPDATA%\hermes\hermes-agent`; ship in repo.
- Codex/other agents hold concurrent WIP — re-check git status before
  finishing; never revert others' work.

---

## PART 1 — Autonomy / exploration freedom

### 1.1 The autonomy budget
A daily budget of self-initiated actions so freedom never becomes runaway
cost or spam. `agent/autonomy/budget.py`: config
`autonomy.{enabled(default true), daily_action_budget(default 8),
per_category: {research: 4, browse: 2, ask_user: 3}}`. Budget resets at
local midnight, persisted `HERMES_HOME/autonomy/budget.json`. Every
autonomous action decrements; exhausted → the action waits for tomorrow (or
becomes a low-priority suggestion). All autonomy actions are logged to
activity.jsonl (source `"autonomy"` — add the enum + UI chip).

### 1.2 Self-directed web research
The subconscious already keeps open questions in the narrative. Give it a way
to *resolve* them autonomously:
- The reflection/tick prompt gains a contract: when the narrative holds an
  open question that web research could answer AND it's worth the budget,
  emit `<research>{"question": ..., "why": ...}</research>`. The scheduler
  hook (where narrative/initiatives are parsed) spawns a bounded research
  sub-agent (reuse `tools/delegate_tool.py` — child AIAgent, web+read
  toolset, no writes) that answers the question; the answer is written to the
  narrative and, if durable, to the graph (Part 2) / brain. Decrements the
  research budget.
- This is Marvi answering its own curiosity between ticks — "I wondered
  whether that Ziraat pattern is his salary; I looked it up in his episodes
  and yes, it recurs monthly."

### 1.3 University student-system visitor
The user wants Marvi to check their Düzce University student system.
- New `plugins/uni_portal/` (a plugin, like smart_room — do NOT bake into
  core). `plugin.yaml` kind backend, `provides_tools: [uni_portal_check]`.
  A browser-automation flow (reuse the existing browser tooling —
  `tools/browser_tool.py` / the CDP browser) that logs in and reads
  grades/announcements/schedule.
- **Credentials: NEVER in config or code.** The plugin uses the OS credential
  store (Windows Credential Manager, the same mechanism smart_room secrets
  use) — the user enrolls once via `hermes uni login` (interactive, stores to
  cred manager). Marvi never sees the password in plaintext; the login flow
  reads it from the store at run time. If the portal has 2FA/CAPTCHA, the flow
  MUST stop and use the ask-user channel (1.4) — never attempt to bypass.
- A daily scheduled check (cron) diffs against last snapshot
  (`HERMES_HOME/uni_portal/snapshot.json`); new grades/announcements → a
  proactive message + episodic + graph node. Config
  `uni_portal.{enabled(default false — off until enrolled), check_schedule}`.
- SECURITY: this is the user's own account, their explicit request, their
  machine, their credentials in their OS store. Document that boundary in
  SKILL.md. No storing of transcripts of the portal beyond the diffed
  snapshot.

### 1.4 The ask-user channel (proactive questions)
Marvi should be able to ASK the user something when genuinely useful — the
most human proactive act.
- `agent/autonomy/ask.py`: `ask_user(question, context, category)` — delivers
  a question through the normal delivery path (the platform the user last
  used / home channel) tagged as a Marvi-initiated question, budgeted
  (per_category.ask_user), deduped (don't re-ask the same open question), and
  RATE-LIMITED hard (never more than N/day, never during a rhythm deep-work
  window unless urgent — reuse flow_gate). The answer, when the user replies,
  is captured back into the narrative/graph (the reply arrives as a normal
  message; correlate by a pending-question store
  `HERMES_HOME/autonomy/pending_questions.json`).
- Reflection/dreaming can emit `<ask>{"question":...,"why":...}</ask>` for
  questions worth surfacing ("You've had 3 late bakery nights this week — want
  me to shift your morning brief later?"). Contradiction-resolution questions
  from decay (Loop 3) route through this same channel instead of only the
  inbox.
- Config `autonomy.ask.{max_per_day(default 3), quiet_in_deep_work(true)}`.

### 1.5 Autonomy in the UI
Mind page "Autonomy" section: today's budget usage per category, recent
autonomous actions (research done, questions asked + answered, portal checks),
and the master + per-category toggles. Pending questions show with their
status. `GET /api/autonomy/status`.

---

## PART 2 — The graph mind (real nodes, edges, relations)

### 2.1 Why + the migration stance
The flat `§` store and separate episodic/brain stores hold Marvi's knowledge
as disconnected rows. A graph makes relations first-class: *Shereef —works_on→
NeuDocs —built_with→ Marvi*; *bakery-job —funds→ NeuDocs*; *"dislikes growing
someone else's income" —motivates→ product-income-goal*. This is what the user
means by "a real tree mind with nodes and connections."

**Stance: additive graph layer over existing stores, NOT a destructive
rewrite.** The `§` files and episodic.db stay the durable source of truth for
their content; the graph indexes and RELATES them. A node references its
source entry (memory hash / episode id / brain doc). This keeps every existing
loop working while adding the relational layer.

### 2.2 Store — `agent/memory/graph.py`
SQLite at `HERMES_HOME/memory/graph.db` (dependency-free, consistent with
episodic/brain — NO external graph DB):
- `nodes(id, type, label, summary, source_kind, source_ref, salience REAL,
  created_at, updated_at, last_surfaced)` — type ∈ person|project|fact|event|
  preference|place|topic|goal|device|org.
- `edges(id, src_id, dst_id, relation, weight REAL, source_ref, created_at)` —
  relation ∈ works_on|related_to|contradicts|caused_by|prefers|part_of|
  located_in|funds|motivates|mentions|happened_at (extensible enum).
- `nodes_fts` (FTS5 over label+summary).
- API: `upsert_node(type,label,summary,...) -> id` (dedup by (type, normalized
  label)); `add_edge(src,dst,relation,weight=1.0)`; `neighbors(node_id,
  depth=1)`; `query(text=|type=|limit=)`; `subgraph(node_id, depth=2) ->
  {nodes,edges}`; `merge_nodes(a,b)`; all guarded, never raise.

### 2.3 Population — the graph builds itself
- **From memory**: a builder pass (runs inside the nightly reflection AND on
  demand) reads semantic + episodic entries and extracts entities+relations
  via the aux model (reuse the auxiliary model routing —
  `auxiliary.graph_builder.{provider,model}` or reuse background_review's
  selector) in a bounded batch. Idempotent by source_ref. Only high-confidence
  relations are written; low-confidence become suggestions? NO — graph
  internal build is autonomous (it references sources, is non-destructive, and
  decay/merge cleans it), but a relation that asserts something the user would
  dispute (a contradiction edge) surfaces via the ask-user channel.
- **From the world**: episodic events, room transitions, uni grades, collected
  docs each upsert their node + link to the entities they mention.
- **Dreaming** consolidates the graph too: merge duplicate nodes, strengthen
  frequently-co-occurring edges, prune orphan/low-salience nodes (archive,
  never hard-delete — consistent with decay).

### 2.4 Graph-aware recall
- New tool `recall_graph(query, depth=1)` (toolset memory, register via
  `from tools.registry import registry`; smoke test guards it) — returns a
  node + its neighborhood as readable relations, so the agent can answer
  "what's connected to NeuDocs" or "why does he want product income."
- The system-prompt memory injection MAY include a compact "relevant
  neighborhood" for entities in the current context (budget-bounded, behind
  `memory.graph.inject_neighborhood` default true) — this is the graph
  actually making Marvi smarter, not just a pretty UI.

### 2.5 The UI — an actual mind map
Mind page gets a **Graph** tab: an interactive force-directed node-graph
(nodes colored by type, edges labeled by relation, click a node → its summary
+ source + neighbors, search to focus, filter by type). Use a dependency-free
canvas/SVG force layout (no heavy graph lib unless one is already vendored —
check; a small self-contained force-directed renderer is fine and preferred
over adding a big dep). `GET /api/memory/graph?focus=&depth=&type=` returns the
subgraph. Read-only v1 (no editing nodes from the UI), but clicking through to
the source entry works. Empty state explains it fills as Marvi learns.

### 2.6 Config
```yaml
memory:
  graph:
    enabled: true
    inject_neighborhood: true
    build_in_reflection: true
    max_nodes: 5000        # beyond this, dreaming prunes lowest-salience
autonomy:
  enabled: true
  daily_action_budget: 8
  per_category: {research: 4, browse: 2, ask_user: 3}
  ask: {max_per_day: 3, quiet_in_deep_work: true}
uni_portal:
  enabled: false
  check_schedule: "0 18 * * *"
```

## API summary
- GET /api/autonomy/status · POST for toggles
- GET /api/memory/graph?focus=&depth=&type= · recall_graph tool
- activity.jsonl sources: + "autonomy", + "graph" (or reuse reflection for
  graph builds — pick one, note UI chip)
- uni_portal endpoints via the plugin

## Build order
1. **Graph store + population from existing memory** (2.2, 2.3 first pass) —
   the substrate; immediately useful via recall_graph.
2. **Graph UI tab** (2.5) — the visible payoff the user asked for.
3. **Graph-aware recall + neighborhood injection** (2.4) — makes it smarter.
4. **Autonomy budget + self-research** (1.1, 1.2) — freedom, bounded.
5. **Ask-user channel** (1.4) — the proactive-question act; route decay
   contradictions + graph disputes through it.
6. **Uni portal plugin** (1.3) — most sensitive (credentials/browser), last,
   off-by-default until enrolled.
7. **Dreaming consolidates the graph** (2.3 last bullet) — fold into the
   existing dreaming job.

## Non-goals (v1)
External graph database (SQLite only); editing graph nodes from the UI;
credential storage anywhere but the OS store; autonomy that bypasses the
budget or the consent tiers; CAPTCHA/2FA bypass on the uni portal (stop and
ask); replacing the flat/episodic stores (graph is additive, references them).
```
