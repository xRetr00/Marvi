"""Subconscious tick — Marvi's periodic world-diff + goal-aware reasoning pass.

Workstream A's slice of the "subconscious" concept from the
2026-07-09-marvi-subconscious-presence design spec. Owns exactly ONE
built-in cron job (``cron.jobs.create_job`` — NO second job engine) that:

  1. runs a mechanical pre-script (Contract 1:
     ``cron/scripts/subconscious_snapshot.py``, owned by Workstream C) that
     prints the literal line ``NO_CHANGE`` or a human-readable diff. When
     the script prints ``NO_CHANGE``, ``cron.scheduler._parse_wake_gate``
     short-circuits the tick BEFORE the LLM stage — zero LLM cost when
     nothing in the user's world changed.
  2. otherwise runs a stage-2 LLM pass with the diff injected as context
     (via the job's ``script`` field — the standard cron script-injection
     mechanism) plus the active goal store (already in every system
     prompt, see ``agent/system_prompt.py``) and recent memory. The pass
     ends the turn with ``[SILENT]``, a delivered proactive message, or a
     registered suggestion (``cron/suggestions.py``, source="subconscious",
     via the ``suggest_automation`` tool).

``hermes subconscious enable|disable|status`` (``hermes_cli/subconscious.py``)
is the CLI surface. Config keys live under ``subconscious.*`` in
config.yaml per Contract 3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

JOB_NAME = "Subconscious tick"
REFLECTION_JOB_NAME = "Subconscious reflection"
DREAMING_JOB_NAME = "Subconscious dreaming"
DEFAULT_INTERVAL = "20m"
DEFAULT_REFLECTION_SCHEDULE = "30 3 * * *"
# Sunday 04:00 — after the 03:30 nightly reflection, so the weekly
# consolidation reasons over a fully-settled narrative (memory-maturity
# spec, Loop 2).
DEFAULT_DREAMING_SCHEDULE = "0 4 * * 0"
DEFAULT_DREAMING_PROMOTE_MIN_OCCURRENCES = 3
DEFAULT_IDLE_TRIGGER_MINUTES = 15
SNAPSHOT_SHIM_NAME = "subconscious_snapshot.py"

# The real Contract-1 script, owned by Workstream C, lives alongside this
# module in the installed package.
_REAL_SNAPSHOT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "subconscious_snapshot.py"

# Toolsets the tick job is restricted to (via create_job's
# enabled_toolsets) — enough to read/steer goals, register a suggestion,
# and use memory, without paying the token cost of the full default
# toolset on every tick. NOTE: cron-spawned agents can never receive the
# ``cronjob`` toolset (force-disabled by ``_resolve_cron_disabled_toolsets``
# in cron/scheduler.py), so the "auto"-tier auto-create path lives inside
# the ``suggest_automation`` tool handler (tools/goal_tools.py), not here.
# "web" (not "search" — there is no toolset registered under that name;
# web_search/web_extract live under "web", see tools/web.py and
# tools/presence/goblin.py's INVESTIGATION_TOOLSETS for the same name)
# gives the tick the ability to actually look something up while deciding
# whether a diff item is worth surfacing.
_TICK_TOOLSETS = ["goals", "subconscious", "memory", "web"]

# Toolsets the weekly dreaming job is restricted to. Unlike the tick this is
# an inward sweep over accumulated memory, not a reaction to a world diff, so
# "web" is dropped and "session_search" added:
#   - "memory": the durable-memory write tool (tools/memory_tool.py, name
#     "memory") for writing high-confidence repeated facts DIRECTLY, plus
#     recall_episode (registered under this toolset in tools/episodic_tool.py)
#     to re-query the episodic log while consolidating.
#   - "session_search": FTS recall over past sessions (tools/session_search_tool.py).
#   - "subconscious": suggest_automation / suggest_goal — the consent-first
#     inbox path for proposing memory/goal/automation CHANGES rather than
#     applying them silently.
#   - "goals": read the active goal store while reasoning about what repeated.
_DREAMING_TOOLSETS = ["memory", "session_search", "subconscious", "goals"]

NARRATIVE_CAP = 8_000
_NARRATIVE_RE = re.compile(
    r"<\s*narrative\s*>\s*(.*?)\s*<\s*/\s*narrative\s*>",
    re.DOTALL | re.IGNORECASE,
)
_INITIATIVES_RE = re.compile(r"<initiatives>\s*(.*?)\s*</initiatives>", re.DOTALL | re.IGNORECASE)
_INITIATIVE_RESULTS_RE = re.compile(
    r"<initiative-results>\s*(.*?)\s*</initiative-results>", re.DOTALL | re.IGNORECASE
)
_NOTICE_RE = re.compile(
    r"<notice(?:\s+urgency=[\"'](normal|urgent)[\"'])?\s*>(.*?)</notice>",
    re.DOTALL | re.IGNORECASE,
)
# Autonomy contract (autonomy spec §1.2/§1.4) — see the <research>/<ask>
# sentences appended to _REFLECTION_PROMPT / _DREAMING_PROMPT below. Parsed
# by extract_autonomy_requests(); cron/scheduler.py's autonomy hook (NOT this
# module — see that file's ownership note) is what actually spends budget and
# spawns a research subagent / calls ask_user for each extracted item.
_RESEARCH_RE = re.compile(r"<research>\s*(.*?)\s*</research>", re.DOTALL | re.IGNORECASE)
_ASK_RE = re.compile(r"<ask>\s*(.*?)\s*</ask>", re.DOTALL | re.IGNORECASE)

_TICK_PROMPT = (
    "[Subconscious tick] You woke up on your own schedule, not because the "
    "user messaged you. Any '## Script Output' block above this message is "
    "a mechanical diff of what changed in the user's world since the last "
    "tick (email, calendar, code activity, or other connected surfaces) — "
    "you only reached this prompt because something changed; NO_CHANGE "
    "ticks are filtered out before you're woken. Your active goals are "
    "listed in your system prompt.\n\n"
    "Decide what, if anything, deserves the user's attention right now:\n"
    "- If the diff is noise, or nothing in it advances an active goal or "
    "matters to the user, respond with exactly \"[SILENT]\" and nothing "
    "else.\n"
    "- If something is genuinely worth a short proactive nudge, emit exactly "
    "one <notice urgency=\"normal\">...</notice> block. Use urgency=\"urgent\" "
    "only for a time-sensitive safety, security, or account risk. The notice "
    "must be natural spoken English, one to three short sentences, with no "
    "Markdown, bullets, XML inside it, or raw internal identifiers. Skip "
    "anything you already told the user.\n"
    "- If the right move is a new recurring automation rather than a "
    "one-off interruption, call suggest_automation to propose it — never "
    "attempt to create the job yourself. The tool is consent-first: it "
    "registers a pending suggestion the user accepts with one tap, and "
    "only auto-creates when the user pre-approved the category as an "
    "'auto' tier in subconscious.tiers.\n"
    "Interpret movement chronologically across the current context and durable "
    "narrative: for example, leaving a non-home zone followed by arriving home "
    "and then room presence is one journey home, not unrelated events. Before "
    "reporting a visitor or device outage, verify the Smart Room evidence: "
    "check the supplied current mode, OwnTracks history, HE20 edge log, and "
    "device health. If that evidence is ambiguous, stay silent. "
    "HE20 movement during active Sleep mode is normal bed movement, not a room "
    "entry. A single failed device poll is transient, not an outage. Never "
    "invent activity that isn't supported by the diff, your goals, or your "
    "memory. Always write in English. End with one compact "
    "<narrative>...</narrative> block that "
    "updates your durable working model. You may also emit JSON arrays inside "
    "<initiatives>...</initiatives> and <initiative-results>...</initiative-results>. "
    "These blocks are persisted and removed before anything is shown to the user."
)

_REFLECTION_PROMPT = (
    "[Nightly subconscious reflection] Quietly consolidate the supplied narrative, "
    "recent activity, goals, suggestions, rhythm and durable memory. Improve the "
    "working model without inventing facts. Infer useful goals from repeated behavior "
    "or memory only as consent-first goal suggestions. If essential intent is uncertain, "
    "ask one short clarifying question in normal prose and do not propose that goal yet. "
    "Never activate a goal without acceptance. Return the refreshed model in exactly one "
    "<narrative>...</narrative> block. Optionally return up to five follow-ups as a JSON "
    "array in <initiatives>...</initiatives>. Once per calendar week, also review "
    "active goals for progress, staleness, duplication, or completion and propose any "
    "change rather than applying it silently.\n\n"
    "Autonomy (spend sparingly — every one of these is budgeted and may be skipped if "
    "today's autonomy budget is already spent): when the narrative holds a genuinely "
    "open question that web research could plausibly resolve, emit "
    "<research>{\"question\": \"...\", \"why\": \"...\"}</research> — this is Marvi "
    "answering its own curiosity between ticks, not a task for the user. When something "
    "is worth proactively asking the user directly (not busywork, not something you "
    "could just infer), emit <ask>{\"question\": \"...\", \"why\": \"...\"}</ask> instead "
    "of only noting it in the narrative. Emit at most one or two of each per run; both "
    "are optional and best-effort — most runs should emit neither."
)

_DREAMING_PROMPT = (
    "[Weekly subconscious dreaming] Review the week the way sleep consolidates "
    "a day. The blocks above give you the week's raw material: the last seven "
    "days of episodes, summaries of recent sessions, the current durable "
    "narrative, your semantic memory (what you already believe about the user), "
    "and the outcomes ledger. Read across all of it, not just the newest items.\n\n"
    "Look for signal that repeats:\n"
    "- What happened more than once — a recurring task, a recurring failure, a "
    "preference the user showed you more than one time.\n"
    "- What you consistently got wrong — corrections, dismissals, or escalations "
    "in the outcomes ledger that point at a wrong assumption you keep making.\n"
    "- Contradictions — places where a newer episode or session conflicts with "
    "an older semantic memory.\n\n"
    "Then act, respecting the evidence bar stated in the '## Consolidation "
    "guidance' block above — promote ONLY patterns with real evidence (seen at "
    "least the stated number of times, or spread across at least that many "
    "distinct days). A single occurrence is an anecdote, not a pattern; leave it "
    "for next week. Concretely:\n"
    "- For a high-confidence repeated FACT about the user (a stable preference, "
    "a durable detail), write it DIRECTLY to durable memory with the memory tool, "
    "tagged to the right topic. That is your job here — do not route a "
    "well-evidenced fact through a suggestion.\n"
    "- For anything the user would want a say in — a memory change that drops or "
    "overrides something, a new or changed goal, a new automation — propose it "
    "through the suggestions inbox (suggest_automation / suggest_goal). Never "
    "activate a goal or create a job yourself.\n"
    "- Note any contradiction you found in the narrative so the decay pass can "
    "reconcile it; do not delete or overwrite the conflicting memory yourself.\n\n"
    "Invent nothing that the episodes, sessions, memory, or ledger do not "
    "support. When you are done, end with exactly one compact "
    "<narrative>...</narrative> block that folds this week's durable conclusions "
    "into your working model. You may also emit up to five follow-ups as a JSON "
    "array in <initiatives>...</initiatives>. These blocks are persisted and "
    "removed before anything is shown to the user.\n\n"
    "Autonomy (spend sparingly, same budgeted contract as reflection): a genuinely "
    "open question the week's material raises that web research could resolve may be "
    "emitted as <research>{\"question\": \"...\", \"why\": \"...\"}</research>. Something "
    "worth proactively asking the user (not busywork) may be emitted as "
    "<ask>{\"question\": \"...\", \"why\": \"...\"}</ask>. At most one or two of each; "
    "most runs should emit neither."
)


def _subconscious_dir() -> Path:
    return get_hermes_home() / "subconscious"


def narrative_path() -> Path:
    return _subconscious_dir() / "narrative.md"


def read_narrative() -> str:
    try:
        return narrative_path().read_text(encoding="utf-8")[:NARRATIVE_CAP]
    except OSError:
        return ""


def read_narrative_history() -> List[Dict[str, Any]]:
    """Return the up-to-3 rotated previous narrative versions, most recent first.

    ``write_narrative`` rotates narrative.md -> .1 -> .2 -> .3 on every write
    (see below); this reads them back for the Mind view's narrative history
    (``GET /api/mind?history=1``). Missing files are skipped rather than
    erroring — a fresh install or one that hasn't ticked three times yet will
    simply have fewer entries.
    """
    path = narrative_path()
    history: List[Dict[str, Any]] = []
    for version in (1, 2, 3):
        candidate = path.with_name(f"{path.name}.{version}")
        try:
            text = candidate.read_text(encoding="utf-8")[:NARRATIVE_CAP]
        except OSError:
            continue
        if text.strip():
            history.append({"version": version, "text": text})
    return history


def write_narrative(text: str) -> None:
    """Atomically persist the bounded narrative and retain three revisions."""
    value = (text or "").strip()[-NARRATIVE_CAP:]
    path = narrative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for index in range(3, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if source.exists():
            try:
                os.replace(source, target)
            except OSError:
                logger.debug("narrative rotation failed", exc_info=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".narrative_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_blocks(pattern: re.Pattern[str], text: str) -> List[Dict[str, Any]]:
    matches = pattern.findall(text or "")
    if not matches:
        return []
    try:
        value = json.loads(matches[-1])
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def process_background_output(text: str) -> Tuple[str, bool]:
    """Persist well-formed private blocks and return delivery-safe prose."""
    raw = text or ""
    narratives = _NARRATIVE_RE.findall(raw)
    updated = False
    if narratives:
        write_narrative(narratives[-1])
        updated = True
    from cron.subconscious_initiatives import add_initiatives, apply_results

    add_initiatives(_json_blocks(_INITIATIVES_RE, raw))
    apply_results(_json_blocks(_INITIATIVE_RESULTS_RE, raw))
    clean = _NARRATIVE_RE.sub("", raw)
    clean = _INITIATIVES_RE.sub("", clean)
    clean = _INITIATIVE_RESULTS_RE.sub("", clean)
    # <research>/<ask> blocks (autonomy spec §1.2/§1.4) are private too —
    # never leaked to delivery. Extraction/spending/spawning happens
    # separately in cron/scheduler.py's autonomy hook (see
    # extract_autonomy_requests), called on the same raw text before this
    # function strips it.
    clean = _RESEARCH_RE.sub("", clean)
    clean = _ASK_RE.sub("", clean)
    clean = _NOTICE_RE.sub(lambda match: match.group(2), clean)
    return clean.strip(), updated


def extract_notice_urgency(text: str) -> str:
    """Return the tick's requested urgency, defaulting safely to normal."""
    matches = _NOTICE_RE.findall(text or "")
    if not matches:
        return "normal"
    return "urgent" if str(matches[-1][0]).lower() == "urgent" else "normal"


def _object_blocks(pattern: "re.Pattern[str]", text: str) -> List[Dict[str, Any]]:
    """Parse each non-overlapping ``<tag>{...}</tag>`` match in ``text`` as a
    JSON object (unlike ``_json_blocks``, which expects a single JSON ARRAY
    match — the autonomy tags can appear multiple times per run, one per
    request). A malformed individual block is skipped rather than failing
    the whole extraction."""
    items: List[Dict[str, Any]] = []
    for raw in pattern.findall(text or ""):
        try:
            obj = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            items.append(obj)
    return items


def extract_autonomy_requests(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract ``<research>``/``<ask>`` JSON objects from raw reflection or
    dreaming output (autonomy spec §1.2/§1.4).

    Call this on the SAME raw text passed to :func:`process_background_output`
    (either before or after — the two operate independently) — see
    ``cron/scheduler.py``'s autonomy hook, which is the actual caller and
    owns spending budget / spawning research / calling ``ask_user`` for each
    returned item. This function only parses; it has no side effects and
    never raises. Returns ``(research_requests, ask_requests)``, each a list
    of ``{"question": ..., "why": ...}``-shaped dicts (unvalidated beyond
    "is a JSON object" — the caller is responsible for checking for a
    non-empty ``question``).
    """
    raw = text or ""
    try:
        research = _object_blocks(_RESEARCH_RE, raw)
    except Exception:
        logger.debug("subconscious: research block extraction failed", exc_info=True)
        research = []
    try:
        ask = _object_blocks(_ASK_RE, raw)
    except Exception:
        logger.debug("subconscious: ask block extraction failed", exc_info=True)
        ask = []
    return research, ask


def _recent_activity_summary(hours: int = 24) -> str:
    path = _subconscious_dir() / "activity.jsonl"
    if not path.exists():
        return "No recent background activity."
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "No recent background activity."
    for line in lines[-200:]:
        try:
            item = json.loads(line)
            at = datetime.fromisoformat(str(item.get("at") or "").replace("Z", "+00:00"))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            rows.append(f"- {item.get('source', 'tick')}: {item.get('summary') or item.get('outcome') or 'completed'}")
    return "\n".join(rows[-30:]) or "No recent background activity."


def dreaming_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``memory.dreaming`` config with inline defaults.

    Uses ``cfg_get`` with inline defaults rather than ``DEFAULT_CONFIG`` —
    the dreaming schedule and evidence threshold are not UI-edited keys.
    Never raises; falls back to the built-in defaults on any read error.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = cfg if cfg is not None else load_config()
        schedule = str(
            cfg_get(cfg, "memory", "dreaming", "schedule", default=DEFAULT_DREAMING_SCHEDULE)
            or DEFAULT_DREAMING_SCHEDULE
        )
        promote_min = _coerce_nonnegative_int(
            cfg_get(cfg, "memory", "dreaming", "promote_min_occurrences",
                    default=DEFAULT_DREAMING_PROMOTE_MIN_OCCURRENCES),
            DEFAULT_DREAMING_PROMOTE_MIN_OCCURRENCES,
        )
        return {"schedule": schedule, "promote_min_occurrences": promote_min}
    except Exception:
        logger.debug("dreaming: config read failed, using defaults", exc_info=True)
        return {
            "schedule": DEFAULT_DREAMING_SCHEDULE,
            "promote_min_occurrences": DEFAULT_DREAMING_PROMOTE_MIN_OCCURRENCES,
        }


def _dreaming_episodes_block(days: int = 7) -> str:
    """Last ``days`` days of episodes (Loop 1), newest first — the primary
    raw material for the weekly consolidation. Bounded and best-effort."""
    try:
        from agent.memory.episodic import format_episode, query

        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        episodes = query(since=since, limit=200)
        if not episodes:
            return "No episodes recorded in the last 7 days."
        return "\n".join(format_episode(ep) for ep in episodes)
    except Exception:
        logger.debug("dreaming: episodes block unavailable", exc_info=True)
        return "Recent episodes unavailable."


def _dreaming_sessions_block(limit: int = 12) -> str:
    """Compact summaries of recent sessions via the read-only session FTS
    toolset (browse shape). Titles + previews only — the model can call
    session_search itself to drill in. Best-effort; never raises."""
    try:
        from tools.session_search_tool import session_search

        raw = session_search(limit=max(1, min(int(limit), 10)))
        data = json.loads(raw)
        results = data.get("results") or []
        if not results:
            return "No recent sessions found."
        lines: List[str] = []
        for row in results:
            title = str(row.get("title") or "(untitled)").strip()
            when = str(row.get("started_at") or row.get("last_active") or "").strip()
            preview = str(row.get("preview") or "").strip()
            if len(preview) > 200:
                preview = preview[:197] + "..."
            line = f"- {title}"
            if when:
                line += f" [{when}]"
            if preview:
                line += f" — {preview}"
            lines.append(line)
        return "\n".join(lines)
    except Exception:
        logger.debug("dreaming: sessions block unavailable", exc_info=True)
        return "Recent sessions unavailable."


def _dreaming_semantic_memory_block() -> str:
    """Read-only snapshot of the durable semantic memory (USER.md/MEMORY.md).

    Instantiates a throwaway MemoryStore and reads its formatted snapshot —
    it does NOT mutate the store class or the on-disk memory (writing durable
    memory is the model's job, through the memory tool, during the turn)."""
    try:
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.load_from_disk()
        blocks: List[str] = []
        for target in ("user", "memory"):
            block = store.format_for_system_prompt(target)
            if block:
                blocks.append(block)
        return "\n\n".join(blocks) if blocks else "No durable semantic memory yet."
    except Exception:
        logger.debug("dreaming: semantic memory block unavailable", exc_info=True)
        return "Semantic memory unavailable."


def _dreaming_outcomes_block(days: int = 30) -> str:
    """Compact view of the learning outcomes ledger — recent events plus a
    per-event count, so the consolidation can see what it consistently got
    wrong (corrections/dismissals). Best-effort; never raises."""
    try:
        from agent.learning.outcomes import counts, recent

        tally = counts(days=days)
        rows = recent(days=days, limit=40)
        head = "Counts (last %dd): %s" % (
            days,
            ", ".join(f"{event}={n}" for event, n in tally.items() if n) or "none",
        )
        if not rows:
            return head + "\nNo individual outcomes recorded."
        lines = [head, "Recent outcomes (newest first):"]
        for row in rows:
            at = str(row.get("at") or "").strip()
            loop = str(row.get("loop") or "").strip()
            event = str(row.get("event") or "").strip()
            category = str(row.get("category") or "").strip()
            entry = f"- [{at}] {loop}/{event}"
            if category:
                entry += f" ({category})"
            lines.append(entry)
        return "\n".join(lines)
    except Exception:
        logger.debug("dreaming: outcomes block unavailable", exc_info=True)
        return "Outcomes ledger unavailable."


def build_dreaming_context(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """Assemble the bounded weekly-consolidation input blocks (memory-maturity
    spec, Loop 2). Returns a list of prose blocks to append to the shared
    runtime context. Each block is independently best-effort — a failure in one
    source degrades to an "unavailable" line, never an empty or broken prompt.
    """
    from agent.goal_store import format_active_goals_for_prompt

    dconfig = dreaming_config(cfg)
    threshold = int(dconfig["promote_min_occurrences"])
    guidance = (
        "## Consolidation guidance\n"
        f"Promote a pattern only with real evidence: seen at least {threshold} "
        f"times, or spread across at least {threshold} distinct days. Below that "
        "bar it is an anecdote — leave it for a later week. High-confidence "
        "repeated facts go straight to durable memory; anything the user would "
        "want a say in goes through the suggestions inbox."
    )
    return [
        guidance,
        f"## Episodes (last 7 days)\n{_dreaming_episodes_block(days=7)}",
        f"## Recent sessions\n{_dreaming_sessions_block()}",
        f"## Semantic memory\n{_dreaming_semantic_memory_block()}",
        f"## Outcomes ledger\n{_dreaming_outcomes_block()}",
        format_active_goals_for_prompt() or "## Active goals\nNone",
    ]


def run_decay_pass_after_dreaming() -> None:
    """Final step of the weekly dreaming job: hand off to the memory decay
    pass (Loop 3, ``agent.memory.decay.run_decay_pass``).

    Guarded import: the decay module is built by a parallel workstream and may
    not exist yet. A missing module is skipped with a single log line — the
    dreaming job works with or without decay present. Any failure inside the
    decay pass itself is likewise swallowed; consolidation must never break
    because the (optional) forgetting step failed.
    """
    try:
        from agent.memory.decay import run_decay_pass
    except ImportError:
        logger.info("dreaming: decay pass module not present yet; skipping decay step")
    else:
        try:
            run_decay_pass()
        except Exception:
            logger.debug("dreaming: decay pass failed", exc_info=True)

    # === Graph-mind maintenance hook (graph-mind spec §2.3 last bullet) ===
    # BEGIN -- small, additive, delimited block placed AFTER the decay seam
    # above (matches the spec's ordering). Merges duplicate nodes then
    # prunes low-salience nodes beyond memory.graph.max_nodes (archiving,
    # never hard-deleting -- see agent.memory.graph.prune_low_salience).
    # Co-occurring edges are already strengthened continuously by
    # agent.memory.graph.add_edge()'s dedup-bump on every repeat write, so
    # there's no separate "strengthen" step here. Guarded + never raises.
    try:
        _run_graph_dreaming_maintenance()
    except Exception:
        logger.debug("dreaming: graph-mind maintenance hook failed", exc_info=True)
    # === Graph-mind maintenance hook -- END =================================


def _run_graph_dreaming_maintenance() -> None:
    """Body of the dreaming graph-maintenance hook (graph-mind spec §2.3
    last bullet): merge near-duplicate nodes, then prune low-salience nodes
    beyond ``memory.graph.max_nodes``. Guarded import mirrors
    ``run_decay_pass_after_dreaming``'s defensive shape; a missing/broken
    graph module is skipped with one log line. Never raises."""
    try:
        from agent.memory.graph import graph_config, prune_low_salience
    except ImportError:
        logger.info("dreaming: graph module not present yet; skipping graph maintenance")
        return
    cfg = graph_config()
    if not cfg.get("enabled", True):
        return
    merged = 0
    try:
        from agent.memory.graph_builder import merge_duplicate_graph_nodes

        merged = merge_duplicate_graph_nodes()
    except Exception:
        logger.debug("dreaming: graph duplicate-merge failed", exc_info=True)
    pruned = 0
    try:
        pruned = prune_low_salience(cfg.get("max_nodes"))
    except Exception:
        logger.debug("dreaming: graph prune failed", exc_info=True)
    logger.info("dreaming: graph maintenance merged=%d pruned=%d", merged, pruned)


def choose_proactive_delivery(
    *,
    phone_home: Optional[bool],
    room_present: bool,
    room_mode: str,
    desktop_afk: str,
    busy: bool,
    urgency: str = "normal",
) -> str:
    """Choose the least disruptive useful channel from current world state."""
    urgent = urgency == "urgent"
    if phone_home is False and desktop_afk != "not-afk":
        return "telegram"
    if room_mode == "sleep" and phone_home is not False:
        return "quiet" if urgent else "defer"
    if busy:
        return "quiet" if urgent else "defer"
    if desktop_afk == "afk":
        return "quiet"
    if room_present or desktop_afk == "not-afk":
        return "speak"
    return "quiet"


def proactive_delivery_context() -> Dict[str, Any]:
    """Return current delivery policy for Desktop and cron transport."""
    phone_home: Optional[bool] = None
    room_present = False
    room_mode = ""
    try:
        from plugins.smart_room.bridge import read_state_snapshot

        room = read_state_snapshot() or {}
        presence = room.get("presence") if isinstance(room.get("presence"), dict) else {}
        location = room.get("location") if isinstance(room.get("location"), dict) else {}
        modes = room.get("modes") if isinstance(room.get("modes"), dict) else {}
        devices = room.get("devices") if isinstance(room.get("devices"), dict) else {}
        bulb = devices.get("tuya_bulb") if isinstance(devices.get("tuya_bulb"), dict) else {}
        he20 = devices.get("tuya_he20") if isinstance(devices.get("tuya_he20"), dict) else {}
        room_present = bool(presence.get("detected"))
        room_mode = str(modes.get("active_mode") or "").strip().lower()
        zone = str(location.get("zone") or "").strip().lower()
        if location.get("home"):
            phone_home = True
        elif zone and zone != "unknown":
            phone_home = False
    except Exception:
        logger.debug("subconscious: delivery room-state probe failed", exc_info=True)

    desktop_afk = ""
    foreground_app = ""
    busy = False
    try:
        import sys

        from hermes_cli.config import cfg_get, load_config
        from tools.presence.common import is_focus_app
        from tools.presence.resource_policy import (
            _is_fullscreen_foreground,
            _win32_foreground_process_name,
        )

        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class _LastInputInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            last_input = _LastInputInfo()
            last_input.cbSize = ctypes.sizeof(_LastInputInfo)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(last_input)):  # type: ignore[attr-defined]
                ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong  # type: ignore[attr-defined]
                idle_ms = ctypes.windll.kernel32.GetTickCount64() - last_input.dwTime  # type: ignore[attr-defined]
                desktop_afk = "afk" if idle_ms >= 120_000 else "not-afk"
            foreground_app = str(_win32_foreground_process_name() or "")
        quiet_apps = cfg_get(
            load_config(),
            "subconscious",
            "delivery",
            "quiet_apps",
            default=[],
        )
        quiet_match = isinstance(quiet_apps, list) and any(
            str(name).strip().lower() in foreground_app.lower()
            for name in quiet_apps
            if str(name).strip()
        )
        busy = is_focus_app(foreground_app) or quiet_match or _is_fullscreen_foreground()
    except Exception:
        logger.debug("subconscious: delivery desktop-state probe failed", exc_info=True)

    normal = choose_proactive_delivery(
        phone_home=phone_home,
        room_present=room_present,
        room_mode=room_mode,
        desktop_afk=desktop_afk,
        busy=busy,
    )
    urgent = choose_proactive_delivery(
        phone_home=phone_home,
        room_present=room_present,
        room_mode=room_mode,
        desktop_afk=desktop_afk,
        busy=busy,
        urgency="urgent",
    )
    return {
        "mode": normal,
        "urgent_mode": urgent,
        "phone_home": phone_home,
        "room_present": room_present,
        "room_mode": room_mode or None,
        "desktop_afk": desktop_afk or None,
        "foreground_app": foreground_app or None,
        "busy": busy,
    }


def build_runtime_context(job_name: str) -> str:
    """Build run-time context without mutating any long-lived chat prefix."""
    from agent.goal_store import format_active_goals_for_prompt
    from cron.subconscious_initiatives import due_initiatives
    from cron.suggestions import list_pending
    from hermes_time import format_timestamp, now as local_now

    narrative = read_narrative() or "No durable narrative yet."
    due = due_initiatives()
    parts = [
        f"## Current local time\n{local_now().strftime('%Y-%m-%d %H:%M %Z')}",
        f"## Durable narrative\n{narrative}",
    ]
    try:
        from plugins.smart_room.bridge import read_state_snapshot
        from plugins.smart_room.runtime.state_store import (
            load_location_reports,
            load_transition_events,
        )

        room = read_state_snapshot() or {}
        presence = room.get("presence") if isinstance(room.get("presence"), dict) else {}
        location = room.get("location") if isinstance(room.get("location"), dict) else {}
        modes = room.get("modes") if isinstance(room.get("modes"), dict) else {}
        movement = [
            (
                f"- {format_timestamp(report.get('reported_at') or report.get('received_at'))}: "
                f"{report.get('event') or 'location'} "
                f"{report.get('zone') or 'outside known regions'}"
            )
            for report in load_location_reports(limit=8)
        ]
        evidence = [
            (
                f"- {format_timestamp(event.get('at'))}: {event.get('type')} "
                f"mode={event.get('mode') or 'unknown'} "
                f"phone_home={event.get('phone_home')} "
                f"classification={event.get('classification') or 'n/a'} "
                f"device={event.get('device') or 'n/a'}"
            )
            for event in load_transition_events()[-12:]
            if event.get("type") in {
                "he20_occupied",
                "he20_cleared",
                "room_entry",
                "room_presence_unverified",
                "device_offline",
                "device_online",
                "mode_changed",
            }
        ]
        parts.append(
            "## Smart Room semantics and recent owner movement\n"
            "Device glossary: tuya_bulb is the room light; tuya_he20 is the "
            "HE20 mmWave human-presence sensor, never a heater or HVAC device. "
            "OwnTracks describes the owner's phone location; ESPresense/BLE and "
            "HE20 describe room occupancy. Read the following reports in time "
            "order and connect a leave -> arrive home -> room-entry sequence as "
            "one journey when the evidence supports it. Short arrive/leave pairs "
            "at non-home regions (including Bakery) can be valid drive-by events; "
            "treat them as route evidence, not necessarily a destination or sensor fault. "
            "All displayed timestamps are in the configured local timezone.\n"
            "The Current line is authoritative over an older durable narrative; "
            "do not describe an offline device as healthy.\n"
            f"Current: phone_home={bool(location.get('home'))}, "
            f"phone_zone={location.get('zone') or 'unknown'}, "
            f"room_present={bool(presence.get('detected'))}, "
            f"room_mode={modes.get('active_mode') or 'none'}, "
            f"tuya_bulb_online={bool(bulb.get('online'))}, "
            f"tuya_he20_online={bool(he20.get('online'))}.\n"
            + ("\n".join(movement) if movement else "- No recent OwnTracks reports.")
            + "\nRecent HE20/mode/device evidence:\n"
            + ("\n".join(evidence) if evidence else "- No recent sensor transitions.")
        )
    except Exception:
        logger.debug("subconscious: smart-room context unavailable", exc_info=True)
    try:
        from tools.presence.common import get_presence_config

        if get_presence_config().get("enabled"):
            from tools.presence.context import desktop_context

            current_desktop = desktop_context("now")
            if current_desktop.get("available"):
                parts.append(
                    "## Current desktop context (ActivityWatch)\n"
                    + json.dumps(current_desktop, ensure_ascii=False)[:3000]
                )
    except Exception:
        logger.debug("subconscious: current desktop context unavailable", exc_info=True)
    if due:
        parts.append("## Due initiatives\n" + json.dumps(due, ensure_ascii=False))
    if job_name == REFLECTION_JOB_NAME:
        # === Graph-mind build hook (graph-mind spec §2.3) -- BEGIN =========
        # Small, additive, delimited block: gives the reflection job a
        # chance to deepen Marvi's knowledge graph (agent/memory/graph.py)
        # from recent semantic + episodic entries before it reasons over the
        # rest of this context. Guarded + config-gated
        # (memory.graph.build_in_reflection, default true); a missing/broken
        # graph module can never block reflection. NOTE for other agents
        # editing this file: this block is intentionally separate from the
        # REFLECTION_PROMPT text and the rest of this branch below -- keep it
        # that way so edits merge cleanly.
        try:
            from agent.memory.graph import graph_config as _graph_config
            from agent.memory.graph_builder import build_graph_from_memory as _build_graph_from_memory

            if _graph_config().get("build_in_reflection", True):
                _build_graph_from_memory()
        except Exception:
            logger.debug("subconscious: graph-mind build hook failed", exc_info=True)
        # === Graph-mind build hook -- END ===================================
        try:
            from agent.learning.reflection import run_reflection

            learning_review = run_reflection()
        except Exception:
            logger.debug("subconscious: learning review unavailable", exc_info=True)
            learning_review = {"error": "unavailable"}
        try:
            from tools.presence.rhythm import rhythm_summary_line

            rhythm = rhythm_summary_line() or "No learned rhythm yet."
        except Exception:
            rhythm = "Rhythm unavailable."
        try:
            from tools.presence.distill import build_digest

            presence_digest = build_digest()[:6000]
        except Exception:
            presence_digest = "Presence digest unavailable."
        try:
            from agent.learning.reflection import episodes_for_prompt

            recent_episodes = episodes_for_prompt(limit=15)
        except Exception:
            logger.debug("subconscious: recent episodes unavailable", exc_info=True)
            recent_episodes = "Recent episodes unavailable."
        parts.extend(
            [
                f"## Last 24 hours\n{_recent_activity_summary()}",
                f"## Presence digest\n{presence_digest}",
                f"## Rhythm\n{rhythm}",
                f"## Recent episodes\n{recent_episodes}",
                format_active_goals_for_prompt() or "## Active goals\nNone",
                "## Deterministic learning review\n" + json.dumps(learning_review, ensure_ascii=False),
                "## Pending suggestions\n" + json.dumps(list_pending(), ensure_ascii=False)[:6000],
            ]
        )
    if job_name == DREAMING_JOB_NAME:
        try:
            parts.extend(build_dreaming_context())
        except Exception:
            logger.debug("subconscious: dreaming context unavailable", exc_info=True)
            parts.append("## Consolidation inputs\nUnavailable.")
    return "\n\n".join(parts)


def _subconscious_cfg(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``subconscious`` config section with Contract 3 defaults filled in."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    section = cfg_get(cfg, "subconscious", default={}) or {}
    if not isinstance(section, dict):
        section = {}
    tiers = section.get("tiers")
    return {
        "enabled": bool(section.get("enabled", False)),
        "interval": str(section.get("interval") or DEFAULT_INTERVAL),
        "idle_trigger_minutes": _coerce_nonnegative_int(
            section.get("idle_trigger_minutes"), DEFAULT_IDLE_TRIGGER_MINUTES
        ),
        "tiers": dict(tiers) if isinstance(tiers, dict) else {},
        "job_id": section.get("job_id"),
        "reflection_job_id": section.get("reflection_job_id"),
        "reflection_schedule": str(section.get("reflection_schedule") or DEFAULT_REFLECTION_SCHEDULE),
        "dreaming_job_id": section.get("dreaming_job_id"),
        "dreaming_schedule": str(
            section.get("dreaming_schedule") or dreaming_config(cfg).get("schedule") or DEFAULT_DREAMING_SCHEDULE
        ),
    }


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def is_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Whether ``subconscious.enabled`` is set."""
    return _subconscious_cfg(cfg)["enabled"]


def idle_trigger_minutes(cfg: Optional[Dict[str, Any]] = None) -> int:
    return _subconscious_cfg(cfg)["idle_trigger_minutes"]


def _normalize_schedule(interval: str) -> str:
    interval = (interval or DEFAULT_INTERVAL).strip()
    if not interval:
        interval = DEFAULT_INTERVAL
    # Accept a bare duration ("20m") or an already-recurring form
    # ("every 20m" / a cron expression) — only prefix "every " for a bare
    # duration so a user-supplied cron expression passes through untouched.
    lowered = interval.lower()
    if lowered.startswith("every ") or " " in interval:
        return interval
    return f"every {interval}"


def _write_snapshot_shim() -> Path:
    """Materialize the Contract-1 pre-run script under HERMES_HOME/scripts/.

    Cron pre-run scripts are sandboxed to ``HERMES_HOME/scripts/`` (see
    ``cron.scheduler._run_job_script``, which rejects any path resolving
    outside it), so the real implementation at
    ``cron/scripts/subconscious_snapshot.py`` inside the installed package
    can't be referenced directly by an absolute path. This writes a tiny
    shim that runs the real script by absolute path via ``runpy`` — no
    import/PYTHONPATH assumptions, so it works the same for source
    checkouts, editable installs, and packaged installs. Regenerated on
    every ``enable()`` so a package upgrade's path is kept current.
    """
    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    shim_path = scripts_dir / SNAPSHOT_SHIM_NAME
    shim_path.write_text(
        (
            '"""Auto-generated shim -- regenerated by `hermes subconscious enable`.\n\n'
            "Runs the real subconscious snapshot script (Contract 1, owned by\n"
            "Workstream C) by absolute path. Cron pre-run scripts are sandboxed to\n"
            "this directory, so the installed package path can't be referenced\n"
            'directly. Do not edit by hand -- edit cron/scripts/subconscious_snapshot.py.\n"""\n'
            "import runpy\n\n"
            f"runpy.run_path({str(_REAL_SNAPSHOT_SCRIPT)!r}, run_name='__main__')\n"
        ),
        encoding="utf-8",
    )
    return shim_path


def enable(interval: Optional[str] = None) -> Dict[str, Any]:
    """Enable the subconscious tick: persist config and create the one cron job.

    Idempotent — if a job is already tracked (``subconscious.job_id``) and
    still exists, this resumes it (if paused) and updates its schedule
    instead of creating a duplicate. "No second job engine" means exactly
    one job total, not one per ``enable()`` call.
    """
    from cron.jobs import create_job, get_job, resume_job, update_job

    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("subconscious") or {})

    resolved_interval = (interval or section.get("interval") or DEFAULT_INTERVAL).strip() or DEFAULT_INTERVAL
    schedule = _normalize_schedule(resolved_interval)
    shim_path = _write_snapshot_shim()

    existing_id = section.get("job_id")
    job = get_job(existing_id) if existing_id else None
    if job is None:
        job = create_job(
            prompt=_TICK_PROMPT,
            schedule=schedule,
            name=JOB_NAME,
            script=shim_path.name,
            deliver="local",
            enabled_toolsets=list(_TICK_TOOLSETS),
        )
        section["job_id"] = job["id"]
    else:
        if job.get("state") == "paused":
            job = resume_job(job["id"]) or job
        updates = {}
        if job.get("schedule_display") != schedule:
            updates["schedule"] = schedule
        for key, value in {
            "prompt": _TICK_PROMPT,
            "script": shim_path.name,
            "enabled_toolsets": list(_TICK_TOOLSETS),
        }.items():
            if key in job and job.get(key) != value:
                updates[key] = value
        if updates:
            try:
                update_job(job["id"], updates)
            except Exception:
                logger.debug("subconscious enable: tick refresh failed", exc_info=True)

    reflection_id = section.get("reflection_job_id")
    reflection = get_job(reflection_id) if reflection_id else None
    reflection_schedule = str(section.get("reflection_schedule") or DEFAULT_REFLECTION_SCHEDULE)
    if reflection is None:
        reflection = create_job(
            prompt=_REFLECTION_PROMPT,
            schedule=reflection_schedule,
            name=REFLECTION_JOB_NAME,
            deliver="local",
            enabled_toolsets=list(_TICK_TOOLSETS),
        )
        section["reflection_job_id"] = reflection["id"]
    else:
        if reflection.get("state") == "paused":
            reflection = resume_job(reflection["id"]) or reflection
        updates = {}
        if reflection.get("schedule_display") != reflection_schedule:
            updates["schedule"] = reflection_schedule
        for key, value in {
            "prompt": _REFLECTION_PROMPT,
            "enabled_toolsets": list(_TICK_TOOLSETS),
        }.items():
            if key in reflection and reflection.get(key) != value:
                updates[key] = value
        if updates:
            update_job(reflection["id"], updates)

    # Weekly dreaming job (memory-maturity spec, Loop 2) — created idempotently
    # in the SAME enable() as the tick + reflection, mirroring the reflection
    # bookkeeping exactly: tracked by ``dreaming_job_id`` in config, resumed +
    # rescheduled if present, never duplicated. The schedule's source of truth
    # is ``memory.dreaming.schedule`` (via ``dreaming_config``); the resolved
    # value is mirrored into the subconscious section for status display.
    dreaming_id = section.get("dreaming_job_id")
    dreaming = get_job(dreaming_id) if dreaming_id else None
    dreaming_schedule = str(section.get("dreaming_schedule") or dreaming_config(cfg).get("schedule") or DEFAULT_DREAMING_SCHEDULE)
    if dreaming is None:
        dreaming = create_job(
            prompt=_DREAMING_PROMPT,
            schedule=dreaming_schedule,
            name=DREAMING_JOB_NAME,
            deliver="local",
            enabled_toolsets=list(_DREAMING_TOOLSETS),
        )
        section["dreaming_job_id"] = dreaming["id"]
    else:
        if dreaming.get("state") == "paused":
            dreaming = resume_job(dreaming["id"]) or dreaming
        updates = {}
        if dreaming.get("schedule_display") != dreaming_schedule:
            updates["schedule"] = dreaming_schedule
        for key, value in {
            "prompt": _DREAMING_PROMPT,
            "enabled_toolsets": list(_DREAMING_TOOLSETS),
        }.items():
            if key in dreaming and dreaming.get(key) != value:
                updates[key] = value
        if updates:
            try:
                update_job(dreaming["id"], updates)
            except Exception:
                logger.debug("subconscious enable: dreaming refresh failed", exc_info=True)

    section["enabled"] = True
    section["interval"] = resolved_interval
    section.setdefault("idle_trigger_minutes", DEFAULT_IDLE_TRIGGER_MINUTES)
    section.setdefault("tiers", {})
    section.setdefault("reflection_schedule", DEFAULT_REFLECTION_SCHEDULE)
    section["dreaming_schedule"] = dreaming_schedule
    cfg["subconscious"] = section
    save_config(cfg)
    return status()


def disable() -> Dict[str, Any]:
    """Disable the subconscious tick: pause the job (if any) and flip config off."""
    from cron.jobs import pause_job

    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    section = dict(cfg.get("subconscious") or {})
    for job_id in (section.get("job_id"), section.get("reflection_job_id"), section.get("dreaming_job_id")):
        if not job_id:
            continue
        try:
            pause_job(job_id, reason="subconscious disabled")
        except Exception:
            logger.debug("subconscious disable: pause_job failed", exc_info=True)
    section["enabled"] = False
    cfg["subconscious"] = section
    save_config(cfg)
    return status()


def status() -> Dict[str, Any]:
    """Return current subconscious config + the tracked job's live state."""
    from cron.jobs import get_job

    section = _subconscious_cfg()
    job = get_job(section["job_id"]) if section.get("job_id") else None
    reflection = get_job(section["reflection_job_id"]) if section.get("reflection_job_id") else None
    dreaming = get_job(section["dreaming_job_id"]) if section.get("dreaming_job_id") else None
    return {
        "enabled": section["enabled"],
        "interval": section["interval"],
        "idle_trigger_minutes": section["idle_trigger_minutes"],
        "tiers": section["tiers"],
        "job_id": section.get("job_id"),
        "job_state": job.get("state") if job else None,
        "last_run_at": job.get("last_run_at") if job else None,
        "next_run_at": job.get("next_run_at") if job else None,
        "reflection_schedule": section["reflection_schedule"],
        "reflection_job_id": section.get("reflection_job_id"),
        "reflection_job_state": reflection.get("state") if reflection else None,
        "reflection_last_run_at": reflection.get("last_run_at") if reflection else None,
        "reflection_next_run_at": reflection.get("next_run_at") if reflection else None,
        "dreaming_schedule": section["dreaming_schedule"],
        "dreaming_job_id": section.get("dreaming_job_id"),
        "dreaming_job_state": dreaming.get("state") if dreaming else None,
        "dreaming_last_run_at": dreaming.get("last_run_at") if dreaming else None,
        "dreaming_next_run_at": dreaming.get("next_run_at") if dreaming else None,
    }


def _should_defer_for_resource_policy() -> bool:
    """True when the presence resource policy says to hold off the tick
    (heavy foreground app -- fullscreen game, video editor, 3D tool).

    Guarded import: ``tools/presence/resource_policy.py`` is a sibling
    workstream's module and any failure to import/evaluate it must never
    block the subconscious tick -- it resolves to "don't defer". Only the
    subconscious tick job is affected; other scheduled cron jobs go through
    the normal ticker untouched.
    """
    try:
        from tools.presence.resource_policy import should_defer_background_work

        return bool(should_defer_background_work())
    except Exception:
        logger.debug("subconscious: resource-policy check failed; not deferring", exc_info=True)
        return False


def _pending_trigger_marker_path() -> Path:
    return get_hermes_home() / "subconscious" / "pending_trigger_reason.json"


def _mark_pending_trigger_reason(reason: str) -> None:
    """Best-effort marker so the activity log (cron/scheduler.py) can
    attribute the next fired tick run to WHY it fired (idle silence vs the
    normal schedule) instead of always logging a plain "tick" source.

    Consumed (read-and-deleted) by
    ``cron.scheduler._consume_pending_trigger_reason`` the moment that run
    completes its wake-gate/agent-completion hook. A stale marker (the
    consumer enforces a max age) is simply ignored rather than mis-attributing
    a later, unrelated regular tick — this is a visibility nicety, never
    allowed to affect the tick itself.
    """
    try:
        path = _pending_trigger_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"reason": reason, "at": _hermes_now().isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("subconscious: failed to write pending-trigger marker", exc_info=True)


def trigger_tick(reason: str = "idle") -> bool:
    """Fire the subconscious tick job once, immediately.

    Reuses the tracked job (``cron.jobs.trigger_job`` just sets
    ``next_run_at`` to now; the existing ticker picks it up on its next
    loop iteration) — no second engine, no direct agent invocation here.
    Returns True iff a trigger was actually issued (subconscious enabled,
    a job is tracked, and the trigger call succeeded).
    """
    section = _subconscious_cfg()
    if not section["enabled"] or not section.get("job_id"):
        return False
    if _should_defer_for_resource_policy():
        logger.info("subconscious: deferring tick (reason=%s) -- heavy foreground app", reason)
        return False
    from cron.jobs import trigger_job

    try:
        job = trigger_job(section["job_id"])
    except Exception:
        logger.debug("subconscious trigger_tick failed", exc_info=True)
        return False
    if job:
        logger.info("subconscious: tick triggered (reason=%s)", reason)
        _mark_pending_trigger_reason(reason)
    return bool(job)
