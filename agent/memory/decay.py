"""Memory decay + contamination control — Loop 3 of the "memory maturity"
round (see ``docs/superpowers/specs/2026-07-17-marvi-memory-maturity-spec.md``).

Marvi's semantic memory (``tools/memory_tool.py``'s USER.md/MEMORY.md) only
ever grows: entries get added, replaced, removed one at a time, but nothing
keeps the store SHARP over months of use. This module adds the missing
counterpart — three passes over the live store:

  a. RECENCY/USAGE SCORING — every entry gets a decayed relevance from its
     age and how recently it was actually surfaced into a prompt (tracked in
     the sidecar ``HERMES_HOME/memory/surfaced.json`` by
     ``tools.memory_tool.stamp_surfaced`` — see that module for why). Entries
     that fall below ``memory.decay.archive_threshold`` AND are older than
     ``memory.decay.min_age_days`` get ARCHIVED (never deleted).
  b. DEDUP/MERGE — near-duplicate entries (cheap text similarity, no
     embeddings) get merged. A pure duplicate (one entry's text is fully
     contained in the other's) is merged autonomously; a near-duplicate pair
     that each carry distinct information is proposed via
     ``cron/suggestions.py`` instead — merging automatically would drop a
     fact, which needs the user's call.
  c. CONTRADICTION FLAGGING — entries in the same topic that directly
     conflict (a shared subject with a differing value, or a shared subject
     where one side negates the other) are flagged via a suggestion. Never
     auto-resolved.

The public entry point is :func:`run_decay_pass` — a no-arg function the
weekly "dreaming" job (Loop 2) calls as its final step, and that can also be
run standalone (e.g. from a CLI command or a test). It NEVER raises: every
failure is logged and swallowed, because a bug in decay must never break the
job that calls it.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def decay_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``memory.decay`` config section with defaults filled in.

    Uses ``cfg_get`` with inline defaults rather than ``DEFAULT_CONFIG`` --
    these keys are not UI-edited (mirrors ``episodic_config``'s reasoning in
    ``agent/memory/episodic.py``). Never raises; falls back to defaults on
    any config-read failure.
    """
    try:
        from hermes_cli.config import cfg_get, load_config

        cfg = config if config is not None else load_config()
        return {
            "enabled": bool(cfg_get(cfg, "memory", "decay", "enabled", default=True)),
            "archive_threshold": float(
                cfg_get(cfg, "memory", "decay", "archive_threshold", default=0.2) or 0.2
            ),
            "min_age_days": int(cfg_get(cfg, "memory", "decay", "min_age_days", default=60) or 60),
            "dedup_similarity": float(
                cfg_get(cfg, "memory", "decay", "dedup_similarity", default=0.85) or 0.85
            ),
        }
    except Exception:
        logger.debug("memory decay: config read failed, using defaults", exc_info=True)
        return {
            "enabled": True,
            "archive_threshold": 0.2,
            "min_age_days": 60,
            "dedup_similarity": 0.85,
        }


# ---------------------------------------------------------------------------
# (a) Recency/usage scoring
# ---------------------------------------------------------------------------

# Half-lives for the two decay components (days). Age is the primary signal
# (an entry gets less "top of mind" simply by getting old); idle-since-
# surfaced is a secondary accelerant. It's secondary today because the store
# still injects its ENTIRE snapshot into every prompt (no selective recall
# yet -- that's Loop 4), so under regular use every entry's last_surfaced is
# refreshed every session and idle_days stays near zero for all entries
# alike. Once Loop 4 makes injection selective, idle_days will start to
# meaningfully differentiate entries; age keeps decay functional in the
# meantime.
_AGE_HALF_LIFE_DAYS = 120.0
_IDLE_HALF_LIFE_DAYS = 90.0
_IDLE_WEIGHT = 0.35
_AGE_WEIGHT = 1.0 - _IDLE_WEIGHT


def relevance_score(age_days: float, idle_days: float) -> float:
    """Pure scoring function: decayed relevance in [0, 1] from age + idle time.

    Exposed standalone (no store/sidecar I/O) so the math is unit-testable
    without fixtures. Both inputs are clamped to >= 0.
    """
    age_days = max(0.0, float(age_days or 0.0))
    idle_days = max(0.0, float(idle_days or 0.0))
    age_component = 0.5 ** (age_days / _AGE_HALF_LIFE_DAYS)
    idle_component = 0.5 ** (idle_days / _IDLE_HALF_LIFE_DAYS)
    return round((_AGE_WEIGHT * age_component) + (_IDLE_WEIGHT * idle_component), 4)


def _days_since(iso_ts: Optional[str], now: datetime) -> float:
    if not iso_ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def _run_recency_pass(store: Any, cfg: Dict[str, Any], result: Dict[str, Any]) -> None:
    from tools.memory_tool import archive_entry, get_surfaced_meta, stamp_surfaced

    now = datetime.now(timezone.utc)
    for target in ("memory", "user"):
        for text in list(store._entries_for(target)):
            meta = get_surfaced_meta(text)
            first_seen = meta.get("first_seen")
            if not first_seen:
                # Never tracked before -- we have no evidence of this
                # entry's true age, so seed the sidecar and treat it as
                # fresh this pass rather than guessing (conservative: an
                # entry can only be archived once we've actually observed
                # it for at least min_age_days).
                stamp_surfaced([text])
                continue

            age_days = _days_since(first_seen, now)
            last_surfaced = meta.get("last_surfaced") or first_seen
            idle_days = _days_since(last_surfaced, now)
            score = relevance_score(age_days, idle_days)

            if score < cfg["archive_threshold"] and age_days >= cfg["min_age_days"]:
                reason = (
                    f"decay: relevance {score:.3f} < threshold {cfg['archive_threshold']} "
                    f"(age {age_days:.0f}d, idle {idle_days:.0f}d)"
                )
                record = archive_entry(store, target, text, reason=reason)
                if record:
                    result["archived"] += 1
                    logger.info("memory decay: archived stale %s entry (%s)", target, reason)


# ---------------------------------------------------------------------------
# (b) Dedup / merge
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def text_similarity(a: str, b: str) -> float:
    """Cheap token/character similarity ratio in [0, 1] -- difflib, no
    embeddings. Exposed standalone for unit tests."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_containment_duplicate(a: str, b: str) -> bool:
    """True when the shorter text is fully contained in the longer one --
    i.e. keeping the longer text loses no information, so merging is safe
    to do autonomously."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    return shorter in longer


def _pick_keep_drop(a: str, b: str) -> Tuple[str, str]:
    """Longer text wins (more info-bearing); ties broken by more recently
    surfaced/seen."""
    if len(a) != len(b):
        return (a, b) if len(a) > len(b) else (b, a)
    from tools.memory_tool import get_surfaced_meta

    meta_a, meta_b = get_surfaced_meta(a), get_surfaced_meta(b)
    ts_a = str(meta_a.get("last_surfaced") or meta_a.get("first_seen") or "")
    ts_b = str(meta_b.get("last_surfaced") or meta_b.get("first_seen") or "")
    return (a, b) if ts_a >= ts_b else (b, a)


def _propose_merge_suggestion(target: str, entry_a: str, entry_b: str) -> bool:
    """Register a propose-tier suggestion for a merge that would drop
    information. Returns True iff a new suggestion was actually created
    (False if latched/duplicate/backlog-full or on any failure)."""
    try:
        from cron.suggestions import add_suggestion
        from tools.memory_tool import entry_hash

        keep, drop = _pick_keep_drop(entry_a, entry_b)
        h1, h2 = sorted([entry_hash(entry_a), entry_hash(entry_b)])
        dedup_key = f"memory-merge:{target}:{h1}:{h2}"
        record = add_suggestion(
            title="Merge overlapping memory entries?",
            description=(
                "Two memory entries overlap heavily but each holds information "
                "the other doesn't, so merging them automatically would drop "
                "something.\n\nKeep: " + keep + "\n\nMerge in: " + drop
            ),
            source="subconscious",
            kind="memory",
            memory_spec={"op": "merge", "target": target, "keep_text": keep, "drop_text": drop},
            dedup_key=dedup_key,
            category="memory",
        )
        return record is not None
    except Exception:
        logger.debug("memory decay: merge suggestion failed", exc_info=True)
        return False


def _run_dedup_pass(store: Any, cfg: Dict[str, Any], result: Dict[str, Any]) -> None:
    from tools.memory_tool import archive_entry

    for target in ("memory", "user"):
        entries = list(store._entries_for(target))
        removed: set = set()
        n = len(entries)
        for i in range(n):
            a = entries[i]
            if a in removed:
                continue
            for j in range(i + 1, n):
                b = entries[j]
                if b in removed or a == b:
                    continue
                sim = text_similarity(a, b)
                if sim < cfg["dedup_similarity"]:
                    continue
                if is_containment_duplicate(a, b):
                    keep, drop = _pick_keep_drop(a, b)
                    record = archive_entry(
                        store, target, drop, reason=f"dedup: merged into kept entry (similarity {sim:.2f})"
                    )
                    if record:
                        removed.add(drop)
                        result["merged"] += 1
                        logger.info(
                            "memory decay: auto-merged duplicate %s entry (similarity %.2f)", target, sim
                        )
                else:
                    if _propose_merge_suggestion(target, a, b):
                        result["merge_suggestions"] += 1


# ---------------------------------------------------------------------------
# (c) Contradiction flagging
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset({
    "the", "and", "that", "this", "with", "from", "have", "has", "had",
    "about", "into", "your", "their", "them", "they", "will", "would",
    "should", "could", "there", "which", "when", "where", "what", "your",
    "user", "prefers", "prefer", "likes", "like", "uses", "using", "works",
})

_NEGATION_MARKERS: Tuple[str, ...] = (
    " not ", " no longer ", "n't ", " never ", " stopped ", " avoid ",
    " hates ", " dislikes ", " without ", " doesn't ", " isn't ", " won't ",
    " can't ", " don't ",
)

_VALUE_PATTERN = re.compile(
    r"^(?P<subject>.+?)\s+(?:is|are|prefers?|uses?|likes?|works?\s+at|lives?\s+in|"
    r"drives?|switched\s+to|moved\s+to|now\s+uses?)\s+(?P<value>.+)$",
    re.IGNORECASE,
)


def _keyword_set(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", _normalize(text))
    return {w for w in words if len(w) > 3 and w not in _STOPWORDS}


def _has_negation(text: str) -> bool:
    padded = f" {_normalize(text)} "
    return any(marker in padded for marker in _NEGATION_MARKERS)


def _extract_subject_value(body: str) -> Optional[Tuple[str, str]]:
    match = _VALUE_PATTERN.match(_normalize(body))
    if not match:
        return None
    subject = re.sub(r"[^\w\s]", "", match.group("subject")).strip()
    value = re.sub(r"[^\w\s]", "", match.group("value")).strip()
    if not subject or not value:
        return None
    return subject, value


def detect_contradiction(entry_a: str, entry_b: str) -> Optional[str]:
    """Return a short human-readable reason if two § entries directly
    conflict, or ``None`` if they don't (or can't be compared).

    Cheap, high-precision heuristic by design (no embeddings, no LLM call):
    both entries must share the same ``[topic]`` prefix (precision guard --
    cross-topic false positives are the main risk with any bag-of-words
    check), and then either:

      1. Match the same "subject VERB value" shape with an IDENTICAL subject
         but a DIFFERENT value ("user drinks coffee" vs "user drinks tea"), or
      2. Share a majority of significant keywords while exactly one side
         carries a negation marker the other doesn't ("user likes email
         notifications" vs "user doesn't like email notifications").

    Exposed standalone for unit tests (precision/recall are the whole point
    of this heuristic, so it needs direct positive/negative-case coverage).
    """
    topic_a, body_a = _split_topic(entry_a)
    topic_b, body_b = _split_topic(entry_b)
    if topic_a != topic_b:
        return None

    sv_a = _extract_subject_value(body_a)
    sv_b = _extract_subject_value(body_b)
    if sv_a and sv_b:
        subject_a, value_a = sv_a
        subject_b, value_b = sv_b
        if subject_a and subject_a == subject_b and value_a != value_b:
            return f"same subject ('{subject_a}') with differing values: '{value_a}' vs '{value_b}'"

    keywords_a, keywords_b = _keyword_set(body_a), _keyword_set(body_b)
    if keywords_a and keywords_b:
        union = keywords_a | keywords_b
        overlap = len(keywords_a & keywords_b) / max(1, len(union))
        if overlap >= 0.5 and _has_negation(body_a) != _has_negation(body_b):
            return "shared subject where one entry negates the other"

    return None


def _split_topic(entry: str) -> Tuple[str, str]:
    from tools.memory_tool import split_topic

    return split_topic(entry)


def _propose_contradiction_suggestion(target: str, entry_a: str, entry_b: str, reason: str) -> bool:
    try:
        from cron.suggestions import add_suggestion
        from tools.memory_tool import entry_hash

        h1, h2 = sorted([entry_hash(entry_a), entry_hash(entry_b)])
        dedup_key = f"memory-contradiction:{target}:{h1}:{h2}"
        record = add_suggestion(
            title="Conflicting memory entries -- which holds?",
            description=(
                f"{reason}\n\n1) {entry_a}\n\n2) {entry_b}\n\n"
                "Marvi won't guess which is current -- reply to say which one "
                "still holds (or both, if they're not actually contradictory)."
            ),
            source="subconscious",
            kind="memory",
            memory_spec={"op": "contradiction", "target": target, "entries": [entry_a, entry_b], "reason": reason},
            dedup_key=dedup_key,
            category="memory",
        )
        return record is not None
    except Exception:
        logger.debug("memory decay: contradiction suggestion failed", exc_info=True)
        return False


def _route_contradiction_to_ask_user(target: str, entry_a: str, entry_b: str, reason: str) -> None:
    """Additive seam (Marvi freedom spec §1.4): also surface a newly-flagged
    contradiction through the autonomy ask-user channel, ALONGSIDE (never
    instead of) the suggestions-inbox proposal this is called after. Fully
    guarded -- ``agent.autonomy`` is a separate workstream and may not be
    importable in every environment; a failure here must never affect the
    suggestion that already succeeded by the time this runs, nor any other
    step of the decay pass. ``ask_user`` does its own budgeting, dedup (by
    question text) and rate-limiting, so this call is safe to make on every
    newly-flagged contradiction without a separate cooldown here.
    """
    try:
        from agent.autonomy.ask import ask_user

        question = (
            "I noticed two things I know about you might conflict "
            f"({reason}). Which one still holds?\n\n1) {entry_a}\n\n2) {entry_b}"
        )
        ask_user(question, context=f"memory contradiction ({target})", category="contradiction")
    except Exception:
        logger.debug("memory decay: ask-user contradiction routing failed", exc_info=True)


def _run_contradiction_pass(store: Any, cfg: Dict[str, Any], result: Dict[str, Any]) -> None:
    from tools.memory_tool import split_topic

    for target in ("memory", "user"):
        entries = list(store._entries_for(target))
        by_topic: Dict[str, List[str]] = {}
        for text in entries:
            topic, _body = split_topic(text)
            by_topic.setdefault(topic, []).append(text)

        for _topic, group in by_topic.items():
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    reason = detect_contradiction(group[i], group[j])
                    if reason and _propose_contradiction_suggestion(target, group[i], group[j], reason):
                        result["contradictions_flagged"] += 1
                        _route_contradiction_to_ask_user(target, group[i], group[j], reason)


# ---------------------------------------------------------------------------
# Orchestration -- the CRITICAL SEAM the dreaming job (Loop 2) calls
# ---------------------------------------------------------------------------


def run_decay_pass() -> Dict[str, Any]:
    """Run the full decay + contamination-control pass over the semantic
    memory store (recency/usage scoring -> dedup/merge -> contradiction
    flagging, in that order so later steps see fewer, cleaner entries).

    No-arg entry point: the weekly "dreaming" job (Loop 2) calls this as its
    final step; it also runs standalone (CLI, tests, manual trigger). NEVER
    raises -- every failure (config, store load, or an individual step) is
    logged and swallowed, so a bug here can never break the caller. Returns
    a summary dict for logging/tests; a disabled pass returns immediately
    with ``enabled: False`` and all counters at 0.
    """
    result: Dict[str, Any] = {
        "enabled": False,
        "archived": 0,
        "merged": 0,
        "merge_suggestions": 0,
        "contradictions_flagged": 0,
        "errors": 0,
    }
    try:
        cfg = decay_config()
        result["enabled"] = cfg["enabled"]
        if not cfg["enabled"]:
            logger.debug("memory decay skipped enabled=false")
            return result

        from tools.memory_tool import load_on_disk_store

        logger.info("memory decay pass started")
        store = load_on_disk_store()

        for step in (_run_recency_pass, _run_dedup_pass, _run_contradiction_pass):
            try:
                step(store, cfg, result)
            except Exception:
                logger.warning("memory decay: step %s failed", getattr(step, "__name__", step), exc_info=True)
                result["errors"] += 1
    except Exception:
        logger.warning("memory decay: run_decay_pass failed", exc_info=True)
        result["errors"] += 1
    logger.info(
        "memory decay pass completed archived=%d merged=%d merge_suggestions=%d "
        "contradictions_flagged=%d errors=%d",
        result["archived"],
        result["merged"],
        result["merge_suggestions"],
        result["contradictions_flagged"],
        result["errors"],
    )
    return result
