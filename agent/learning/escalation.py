"""Voice instant-lane correction detection and compact escalation hints."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable

from hermes_constants import get_hermes_home
from utils import atomic_replace

_MARKER = re.compile(r"^(?:no[, ]|actually\b|i meant\b|that's not\b|that is not\b|you misunderstood\b|not what i)", re.I)
_WORD = re.compile(r"[\w']+")


def is_correction(previous_utterance: str, previous_reply: str, utterance: str) -> bool:
    text = utterance.strip()
    if _MARKER.search(text):
        return True
    current = set(token.casefold() for token in _WORD.findall(text))
    prior = set(token.casefold() for token in _WORD.findall(previous_utterance))
    reply = set(token.casefold() for token in _WORD.findall(previous_reply))
    if len(current) < 3:
        return False
    # A restatement that retains the subject but introduces substantial new
    # wording soon after the answer is a conservative correction signal.
    overlap = len(current & prior) / max(1, len(current | prior))
    contradicted_reply = len(current & reply) / max(1, len(current))
    return overlap >= .25 and contradicted_reply < .5 and len(current - prior) >= 2


def mine(events: Iterable[Dict[str, Any]], *, maximum_examples: int = 5, maximum_chars: int = 600) -> str:
    examples: list[str] = []
    seen = set()
    for event in events:
        if event.get("event") != "corrected":
            continue
        prior = " ".join(str((event.get("detail") or {}).get("prior_utterance") or "").split())
        if not prior:
            continue
        key = prior.casefold()
        if key in seen:
            continue
        seen.add(key)
        examples.append(prior[:120])
        if len(examples) >= maximum_examples:
            break
    if not examples:
        return ""
    header = "Escalate instead of using the instant lane for requests like:\n"
    result = header
    for example in examples:
        candidate = result + f"- {example}\n"
        if len(candidate) > maximum_chars:
            break
        result = candidate
    return result.rstrip()[:maximum_chars]


def hints_path() -> Path:
    return get_hermes_home().resolve() / "learning" / "escalation_hints.txt"


def mine_patterns(corrected_events: Iterable[Dict[str, Any]], *, maximum_examples: int = 5,
                  maximum_chars: int = 600) -> str:
    return mine(corrected_events, maximum_examples=maximum_examples, maximum_chars=maximum_chars)


def write_hints(events: Iterable[Dict[str, Any]], *, maximum_examples: int = 5) -> str:
    content = mine_patterns(events, maximum_examples=maximum_examples)
    if not content:
        return ""
    path = hints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hints_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp, path)
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
    return content


def read_hints() -> str:
    try:
        return hints_path().read_text(encoding="utf-8")[:600]
    except OSError:
        return ""
