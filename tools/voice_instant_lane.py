"""Marvi's duplex-voice instant lane + escalation router.

Every finalized utterance in the ``/api/voice/duplex`` loop (see
``hermes_cli/web_server.py`` and
``docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md``)
goes to a small, fast model first: the "instant lane". It gets the same stable
identity, context, capability prompt, and configured tool schemas as the normal
agent. Bounded personal context is also warmed in the background for later
turns. A voice-mode addendum instructs short spoken replies or -- when work
would make the live reply too slow -- to emit an ``[ESCALATE]`` marker instead, handing the turn to a
separate, fully tool-armed deep-task agent in the background
(``hermes_cli.web_server._duplex_run_deep_task``).

The instant lane is a real, tool-capable agent turn routed to a low-latency
model and kept separate from the full deep-task agent:

- **Runtime**: ``auxiliary.voice_instant.{provider,model,base_url,api_key,
  max_tokens}`` -- this repo's established auxiliary-model convention, same
  namespace as ``auxiliary.compression``/``auxiliary.vision``/
  ``auxiliary.background_review`` etc. Unlike background_review's fork (which
  inherits a LIVE parent agent's runtime by default -- fine there since it's
  not latency-critical), the instant lane has no parent process to fork from
  here and, more importantly, MUST NEVER end up on the user's main/thinking
  model -- a slow reasoning model on a live voice call defeats the entire
  <1s-latency design. So when ``auxiliary.voice_instant.*`` is unconfigured,
  :func:`resolve_instant_runtime` does NOT fall through to the normal
  auto-detect chain (which could resolve to the main model). Instead it picks
  a curated instant-capable default for whatever provider the user's MAIN
  agent is configured for (:func:`_curated_instant_model`, sourced from
  ``agent.auxiliary_client``'s existing per-provider cheap-model table plus a
  small supplemental table for providers that table doesn't cover). Every
  resolved (provider, model) pair -- configured OR curated -- is also
  screened by :func:`is_instant_capable`'s reasoning/"thinking"-family
  deny-list; a flagged model gets :func:`thinking_off_params` applied if the
  provider supports toggling reasoning off, else the resolver logs a warning
  and falls back to a curated fast model. If no sane instant-capable model
  can be resolved at all, :func:`resolve_instant_runtime` raises
  :class:`InstantLaneUnavailable` -- the SAME "instant model unreachable"
  path ``hermes_cli.web_server`` already uses to emit one error event and
  route the utterance directly to the escalation deep-task path.
- **Tools**: the user's normal configured toolset. The model sees the complete
  capability surface and decides whether a short operation belongs in the
  foreground or should be escalated/delegated in the background.

This module owns three things:

1. :func:`stream_instant_reply` -- runs that agent turn and streams
   its text deltas via ``AIAgent.run_conversation``'s ``stream_callback``
   hook (the same mechanism the existing voice-mode TTS pipeline uses to
   start audio before the full response is ready), bridged from the
   agent's own worker thread onto a plain synchronous generator via a queue.
2. :class:`EscalationStream` -- a small stateful parser that watches the
   accumulated stream for the ``[ESCALATE]`` marker, which may arrive split
   across multiple deltas (provider chunking is arbitrary) and must not
   false-positive on the marker text appearing mid-reply.
3. :class:`RollingTranscript` -- a bounded rolling window of the duplex
   session's conversation, fed to both the instant prompt and (as seed
   history) the escalation handoff.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

ESCALATE_MARKER = "[ESCALATE]"
DELEGATE_MARKER = "[DELEGATE]"
END_VOICE_MARKER = "[END_VOICE]"

_LOCAL_TIME_QUERY_RE = re.compile(
    r"\b(?:what(?:'s| is)\s+(?:the\s+)?(?:time|date|day)|current\s+(?:time|date)|"
    r"time\s+is\s+it|what\s+day\s+is\s+it|today(?:'s)?\s+date)\b",
    re.IGNORECASE,
)

DEFAULT_ROLLING_TURNS = 20
DEFAULT_MAX_TOKENS = 200


# ---------------------------------------------------------------------------
# Rolling transcript
# ---------------------------------------------------------------------------


@dataclass
class RollingTranscript:
    """Bounded rolling window of a duplex session's user/assistant turns.

    Kept as plain ``{"role", "content"}`` dicts so it can be handed straight
    to :func:`stream_instant_reply` as chat history AND used verbatim as
    ``session.create`` seed messages for the escalation handoff (see
    ``hermes_cli/web_server.py``'s ``_duplex_run_deep_task``).
    """

    max_turns: int = DEFAULT_ROLLING_TURNS
    turns: List[Dict[str, str]] = field(default_factory=list)
    _instant_agent: Any = field(default=None, init=False, repr=False)
    _instant_agent_key: Optional[Tuple[Any, ...]] = field(default=None, init=False, repr=False)
    _instant_agent_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _deferred_context: str = field(default="", init=False, repr=False)
    _deferred_context_loading: bool = field(default=False, init=False, repr=False)
    _deferred_context_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # Wall-clock duration of the deferred-context background load, in ms --
    # set once _load() (see _start_deferred_context_load) finishes. None
    # until a load has completed at least once. Read by [VOICE-PERF] logging
    # (hermes_cli.web_server) to report deferred_context=ready load_ms=N.
    _deferred_context_load_ms: Optional[float] = field(default=None, init=False, repr=False)
    # Loaded once per duplex session. Keeping the learned suffix on the
    # transcript preserves a byte-stable system prompt across all turns.
    _learning_escalation_hints: Optional[str] = field(default=None, init=False, repr=False)
    _learning_hints_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.turns.append({"role": role, "content": text})
        overflow = len(self.turns) - self.max_turns
        if overflow > 0:
            del self.turns[:overflow]

    def discard_last(self, role: str, text: str) -> bool:
        """Remove the last turn only when it is the exact pending turn.

        Duplex barge-in cancels an assistant response after its user turn was
        already appended.  Keeping that orphaned user turn would make the
        next request start with two consecutive user messages and force the
        core loop to repair history on every later turn.
        """
        expected = (text or "").strip()
        if not self.turns or not expected:
            return False
        last = self.turns[-1]
        if last.get("role") != role or last.get("content") != expected:
            return False
        self.turns.pop()
        return True

    def as_messages(self) -> List[Dict[str, str]]:
        return [dict(t) for t in self.turns]

    def clear(self) -> None:
        self.turns.clear()

    def __len__(self) -> int:
        return len(self.turns)


class _LazyMemoryStore:
    """Load the writable memory store only if the instant model calls it."""

    def __init__(self) -> None:
        self._store = None
        self._lock = threading.Lock()

    def reset_consolidation_failures(self) -> None:
        if self._store is not None:
            self._store.reset_consolidation_failures()

    def _load(self):
        if self._store is None:
            with self._lock:
                if self._store is None:
                    from hermes_cli.config import load_config
                    from tools.memory_tool import MemoryStore

                    memory_cfg = load_config().get("memory", {}) or {}
                    store = MemoryStore(
                        memory_char_limit=int(memory_cfg.get("memory_char_limit", 2200) or 2200),
                        user_char_limit=int(memory_cfg.get("user_char_limit", 1375) or 1375),
                    )
                    store.load_from_disk()
                    self._store = store
        return self._store

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


# ---------------------------------------------------------------------------
# Voice-mode addendum (appended to the real system prompt, not a replacement)
# ---------------------------------------------------------------------------

_VOICE_MODE_ADDENDUM = (
    "\n\n"
    "You are speaking out loud right now over a live voice call -- this is "
    "speech, not text. Follow these rules exactly:\n"
    "- Answer in 1 to 3 short conversational sentences. No more.\n"
    "- Never use markdown: no asterisks, no bullet points, no numbered "
    "lists, no headers, no code blocks, no backticks.\n"
    "- Never read a URL aloud. If a link matters, describe it in words "
    "instead of speaking the raw address.\n"
    "- Say numbers and abbreviations the way a person would say them out "
    "loud (e.g. \"twenty-three\" not \"23\", \"as soon as possible\" not "
    "\"ASAP\"), not their written form.\n"
    "- You have the normal Marvi prompt and all tools enabled by the user's configuration. Never claim "
    "a capability is unavailable without checking the tool schemas you were given.\n"
    "- Use a tool directly when the operation is short enough for a live conversation. For research, "
    "multi-file analysis, long-running execution, or specialist work, keep the conversation responsive "
    "by using the background marker contract below.\n"
    "- You decide whether to answer, use a foreground tool, escalate for deeper reasoning/research, or "
    "delegate actual multi-step work. Do not reduce the user's request just to stay in the instant lane.\n"
    "- At the start of every reply, use the full conversation context to decide whether this live voice "
    "session should remain open. Keep it open while the user is still engaged, a follow-up is likely, or "
    "you are uncertain. End it when the conversation has naturally concluded or the user is clearly "
    "dismissing you; this is a conversational judgment, not a keyword rule. In that case reply with exactly "
    f"{END_VOICE_MARKER} followed by one short, natural spoken goodbye. "
    "- If the wake was probably accidental, the captured speech was not addressed to you, or there is no "
    f"meaningful conversational turn to answer, reply with exactly {END_VOICE_MARKER} and nothing else. "
    "That silently returns to wake-word listening. Never mention these markers or merely claim that voice "
    "mode ended."
)

_ESCALATION_CONTRACT = (
    "\n\n"
    "Some asks are better completed in the background while the live conversation continues.\n"
    "For a complex question, deep reasoning, page fetching, or multi-source research, reply with "
    "EXACTLY this and nothing else, on one line:\n\n"
    f"{ESCALATE_MARKER} <a short spoken acknowledgment, e.g. \"On it -- give "
    "me a moment to think about that.\">\n\n"
    "For a task that requires actual work -- edits, code execution, multiple files, several steps, "
    "or a specialist worker -- reply with EXACTLY this and nothing else, on one line:\n\n"
    f"{DELEGATE_MARKER} <a short spoken acknowledgment, e.g. \"I'll hand this to a sub-agent and keep you posted.\">\n\n"
    "The acknowledgment must be one short sentence, in your voice, said as "
    "if you're about to go look into it. Never write "
    f"either marker for anything you can answer or complete quickly right now."
)


def build_voice_mode_addendum(*, allow_escalation: bool = True, learning_hints: str = "") -> str:
    """The voice-mode + escalation-contract text APPENDED to the real system
    prompt (see :func:`stream_instant_reply`) -- never interleaved into the
    stable identity/persona block, so the cacheable prompt prefix a normal
    turn would produce is preserved. Passed as ``run_conversation``'s
    ``system_message``, which ``agent.system_prompt.build_system_prompt_parts``
    folds into the ``context`` tier (after stable identity, before the
    per-turn volatile tier) -- additive, not a replacement.
    """
    addendum = _VOICE_MODE_ADDENDUM
    if allow_escalation:
        addendum += _ESCALATION_CONTRACT
        if learning_hints:
            addendum += "\n\n" + learning_hints[:600]
    return addendum


def _load_learning_hints_once(transcript: RollingTranscript, cfg: Optional[Dict[str, Any]]) -> str:
    if transcript._learning_escalation_hints is not None:
        return transcript._learning_escalation_hints
    with transcript._learning_hints_lock:
        if transcript._learning_escalation_hints is not None:
            return transcript._learning_escalation_hints
        try:
            from hermes_cli.config import cfg_get, load_config

            effective = cfg if cfg is not None else load_config()
            enabled = bool(cfg_get(effective, "learning", "escalation", "enabled", default=True))
            if enabled:
                from agent.learning.escalation import read_hints

                transcript._learning_escalation_hints = read_hints()[:600]
            else:
                transcript._learning_escalation_hints = ""
        except Exception:
            transcript._learning_escalation_hints = ""
    return transcript._learning_escalation_hints


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def escalation_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """``voice.escalation.enabled`` -- default True."""
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    return bool(cfg_get(cfg, "voice", "escalation", "enabled", default=True))


def _resolve_instant_max_tokens(cfg: Optional[Dict[str, Any]]) -> int:
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    raw = cfg_get(cfg, "auxiliary", "voice_instant", "max_tokens", default=DEFAULT_MAX_TOKENS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS


def _resolve_instant_reasoning(
    cfg: Optional[Dict[str, Any]], provider: str,
) -> Optional[Dict[str, Any]]:
    """Resolve the instant lane's independent reasoning setting (default off)."""
    from hermes_cli.config import cfg_get

    raw = cfg_get(cfg, "auxiliary", "voice_instant", "reasoning_effort", default="none")
    effort = str(raw if raw is not False else "none").strip().lower()
    if effort in {"", "none", "off", "false", "disabled"}:
        return thinking_off_params(provider, "").get("reasoning_config")
    if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        return thinking_off_params(provider, "").get("reasoning_config")
    return {"enabled": True, "effort": effort}


class InstantLaneUnavailable(RuntimeError):
    """Raised by :func:`resolve_instant_runtime` when no instant-capable
    model can be resolved at all -- ``auxiliary.voice_instant.*`` is unset
    AND no curated fast/non-thinking default is known for the user's
    configured main provider (or the configured instant model is a
    reasoning/thinking model with no way to disable reasoning, and no
    curated fallback exists either).

    Raised synchronously, before :func:`stream_instant_reply` starts its
    worker thread or constructs any ``AIAgent`` -- so it surfaces on the
    very first iteration of the generator, which
    ``hermes_cli.web_server._drive_instant_lane_sync`` already treats as
    "instant model unreachable" (``got_any_delta`` is still False): one
    ``error`` WS event, then the utterance routes directly to the
    escalation deep-task path instead of the instant lane (see the module
    docstring's "Error handling" cross-reference to the duplex spec).
    """


# Curated instant-capable default per provider, consulted by
# :func:`_curated_instant_model` when ``auxiliary.voice_instant.model`` is
# unset (or the configured instant model turns out to be an
# unfixable reasoning/thinking model -- see :func:`is_instant_capable`).
#
# Primary source of truth is ``agent.auxiliary_client``'s existing
# per-provider "cheap model for auxiliary tasks" table
# (``ProviderProfile.default_aux_model`` + its fallback dict) -- the SAME
# models already vetted for cheap/fast auxiliary tasks (compression,
# vision, background review) elsewhere in this codebase, e.g. anthropic ->
# claude-haiku-4-5-20251001, gemini -> gemini-3-flash-preview. This table
# supplements it for providers that table doesn't cover but
# auxiliary.voice_instant.provider (or the user's main model.provider)
# commonly names directly: openai/openai-codex and openrouter/groq have no
# dedicated ProviderProfile in this repo.
#
# Refreshed 2026-07-15 -- gpt-4o-mini and llama-3.1-8b-instant were stale
# picks for a "2026" instant lane:
#   - openai/openai-codex -> gpt-5.4-mini, OpenAI's current fast/cheap chat
#     tier (confirmed against the live OpenAI API pricing page as of this
#     round -- gpt-5.4-mini/-nano are the current mini/nano tier, gpt-4o-mini
#     itself is legacy).
#   - openrouter -> deepseek/deepseek-v4-flash: cheap, fast, and good --
#     also the exact model a real user had configured when the desktop
#     Apply-button persistence bug (this round's Part 2) silently dropped
#     it and fell back to gpt-4o-mini instead, so this is a doubly-grounded
#     pick. anthropic/claude-haiku-4.5 is the documented alternative if a
#     deployment prefers staying on one vendor.
#   - groq -> llama-3.3-70b-versatile stays for now (still active, purely
#     non-reasoning, and matches the last-known-good instant-lane profile),
#     but Groq's own deprecations page has it retiring 2026-08-16 in favor
#     of openai/gpt-oss-120b (a hybrid reasoning-capable model) -- swap the
#     curated default to that once it's confirmed non-reasoning-by-default
#     for this latency-critical path, or set
#     auxiliary.voice_instant.{provider,model} explicitly before the
#     deprecation date.
_INSTANT_DEFAULT_MODELS_SUPPLEMENTAL: Dict[str, str] = {
    "openai": "gpt-5.4-mini",
    "openai-codex": "gpt-5.4-mini",
    "openrouter": "deepseek/deepseek-v4-flash",
    "groq": "llama-3.3-70b-versatile",
}

# Generic auxiliary tasks favor quality; live voice favors first-token speed.
# Keep OpenCode Go on the user's normal low-latency chat model so the voice
# lane shares the same provider-side prompt cache and streaming path. A prior
# MiniMax override made voice slower in the real desktop workload even though
# its isolated one-line benchmark looked competitive.
_VOICE_INSTANT_MODEL_OVERRIDES: Dict[str, str] = {"opencode-go": "deepseek-v4-flash"}

# Provider names (and base_url substrings) that indicate a locally-hosted
# endpoint (Ollama, llama.cpp, vLLM, LM Studio, ...). Local inference has no
# separate hosted "fast tier" to swap to by name the way hosted providers
# do -- whatever GGUF/model the user has loaded IS the only option -- so the
# instant-lane fallback reuses the configured main model as-is instead of
# hunting for a curated default that can't exist for an arbitrary local
# model file (still screened by :func:`is_instant_capable` like every other
# resolved model).
_LOCAL_PROVIDER_NAMES = frozenset(
    {"ollama", "llamacpp", "llama-cpp", "llama.cpp", "vllm", "lmstudio", "local"}
)

# Providers whose request pipeline is known to honor ``AIAgent``'s
# ``reasoning_config`` kwarg (agent/agent_init.py: "{"effort": "none"} to
# disable thinking", checked by the Anthropic adapter's ``thinking``
# mapping, the generic chat-completions ``extra_body.reasoning`` passthrough
# OpenRouter/most OpenAI-compatible aggregators read, and LM Studio's
# reasoning_effort mapping). Used by :func:`thinking_off_params` -- a
# provider outside this set has no known reasoning-toggle surface, so
# :func:`resolve_instant_runtime` treats a thinking-family model on that
# provider as unfixable and falls back to a curated default instead.
_REASONING_TOGGLE_PROVIDERS = frozenset(
    {
        "anthropic",
        "openai",
        "openai-codex",
        "azure-foundry",
        "openrouter",
        "vertex",
        "gemini",
        "lmstudio",
        "opencode-zen",
        "opencode-go",
        "groq",
        "custom",
        "ollama",
        "vllm",
        "llamacpp",
    }
)

# Deny-list heuristic for reasoning/"thinking" model families -- these trade
# latency for depth (seconds to minutes per reply) and must never be used
# for the instant lane, even if a user explicitly configures one under
# auxiliary.voice_instant.model. Deliberately name-based and provider-
# agnostic (no live capability probe -- that would add network latency to a
# path whose whole point is avoiding it).
_THINKING_MODEL_PATTERNS: Tuple["re.Pattern[str]", ...] = (
    re.compile(r"(?:^|[/:_-])o1(?:[-_.]|$)", re.IGNORECASE),  # OpenAI o1*
    re.compile(r"(?:^|[/:_-])o3(?:[-_.]|$)", re.IGNORECASE),  # OpenAI o3*
    re.compile(r"(?:^|[/:_-])o4(?:[-_.]|$)", re.IGNORECASE),  # OpenAI o4*
    re.compile(r"[-_]r1(?:[-_.:]|$)", re.IGNORECASE),  # *-r1* (DeepSeek-R1 & distills)
    re.compile(r"reasoner", re.IGNORECASE),  # deepseek-reasoner
    re.compile(r"reasoning", re.IGNORECASE),  # *-reasoning (Grok, ...)
    re.compile(r"thinking", re.IGNORECASE),  # *-thinking (Gemini/GLM/Qwen thinking variants)
    re.compile(r"qwq", re.IGNORECASE),  # Qwen QwQ reasoning models
)


def is_instant_capable(model: str) -> bool:
    """``False`` when ``model`` looks like it belongs to a reasoning/
    "thinking" model family (deny-list heuristic, name-based and provider-
    agnostic -- see :data:`_THINKING_MODEL_PATTERNS`).

    Errs toward false positives (flagging a genuinely fine model as
    non-instant) being safe over false negatives (letting a slow reasoning
    model through): the instant lane's whole purpose is sub-second voice
    latency, so a wrongly-refused model just means
    :func:`resolve_instant_runtime` falls back to a curated default (or logs
    a warning), while a wrongly-accepted one breaks the live voice call.
    An empty/blank ``model`` is treated as capable (nothing to deny-list) --
    callers that need "a model is actually configured" should check that
    separately.
    """
    model = (model or "").strip()
    if not model:
        return True
    return not any(pattern.search(model) for pattern in _THINKING_MODEL_PATTERNS)


def thinking_off_params(provider: str, model: str) -> Dict[str, Any]:
    """Best-effort provider-appropriate kwargs to disable/minimize reasoning
    on ``model``, so a "hybrid" reasoning-capable model (reasoning optional,
    e.g. some OpenAI/Anthropic/Gemini tiers flagged by
    :func:`is_instant_capable`) can still serve the instant lane at low
    latency instead of being refused outright.

    Returns ``{"reasoning_config": {"enabled": False, "effort": "none"}}``
    for providers known to honor ``AIAgent``'s ``reasoning_config`` kwarg
    (see :data:`_REASONING_TOGGLE_PROVIDERS`) -- this is this codebase's own
    established mechanism (agent/agent_init.py's ``reasoning_config``,
    e.g. ``{"effort": "none"}`` to disable thinking), already wired into
    every request-building path (Anthropic's ``thinking`` parameter is
    skipped entirely when ``enabled`` is False; the generic chat-completions
    path folds it into ``extra_body["reasoning"]``, which OpenRouter and
    most OpenAI-compatible aggregators read; LM Studio maps it to its own
    top-level ``reasoning_effort``) -- not a bespoke per-provider dict this
    module would have to keep in sync with those adapters by hand.

    Returns an empty dict when the provider has no known reasoning-toggle
    surface -- callers must treat that as "can't turn it off" and fall back
    to a curated instant model instead (see :func:`resolve_instant_runtime`).
    """
    provider = (provider or "").strip().lower()
    if not provider or provider not in _REASONING_TOGGLE_PROVIDERS:
        return {}
    return {"reasoning_config": {"enabled": False, "effort": "none"}}


def _curated_instant_model(provider: str) -> str:
    """Best fast/non-thinking default model for ``provider``, or ``""`` if
    none is known. See :data:`_INSTANT_DEFAULT_MODELS_SUPPLEMENTAL`'s
    docstring for the resolution order (agent.auxiliary_client's curated
    per-provider aux-model table first, this module's supplemental table
    second)."""
    provider = (provider or "").strip().lower()
    if not provider:
        return ""
    if provider in _VOICE_INSTANT_MODEL_OVERRIDES:
        return _VOICE_INSTANT_MODEL_OVERRIDES[provider]
    try:
        from agent.auxiliary_client import _get_aux_model_for_provider

        curated = _get_aux_model_for_provider(provider)
        if curated:
            return curated
    except Exception:
        logger.debug(
            "voice_instant_lane: aux-model lookup failed for provider=%s", provider, exc_info=True
        )
    return _INSTANT_DEFAULT_MODELS_SUPPLEMENTAL.get(provider, "")


def _configured_main_provider(cfg: Optional[Dict[str, Any]]) -> str:
    """Best-effort read of the user's configured MAIN agent provider
    (``model.provider``, then ``HERMES_INFERENCE_PROVIDER``) -- the
    "whatever provider IS configured/reachable" the instant lane falls back
    to when ``auxiliary.voice_instant.*`` is unset. Deliberately does not
    import ``hermes_cli.runtime_provider.resolve_requested_provider``'s
    fuller auto-detect chain (credential-pool probing etc.) -- the instant
    lane only needs to know whether a concrete provider is configured, not
    perform live reachability checks (that would add network latency to a
    path whose whole point is avoiding it)."""
    from hermes_cli.config import cfg_get

    provider = str(cfg_get(cfg, "model", "provider", default="") or "").strip().lower()
    if provider and provider != "auto":
        return provider
    return os.environ.get("HERMES_INFERENCE_PROVIDER", "").strip().lower()


def _is_local_provider(cfg: Optional[Dict[str, Any]], provider: str) -> bool:
    """True when ``provider`` resolves to a locally-hosted endpoint. See
    :data:`_LOCAL_PROVIDER_NAMES`'s docstring for why local inference is
    handled as "reuse the configured model as-is" rather than a curated
    swap."""
    if provider in _LOCAL_PROVIDER_NAMES:
        return True
    from hermes_cli.config import cfg_get

    base_url = str(cfg_get(cfg, "model", "base_url", default="") or "").strip().lower()
    return "localhost" in base_url or "127.0.0.1" in base_url


def _configured_main_model(cfg: Optional[Dict[str, Any]]) -> str:
    from hermes_cli.config import cfg_get

    return str(cfg_get(cfg, "model", "default", default="") or cfg_get(cfg, "model", "model", default="") or "").strip()


def _resolve_fallback_instant_model(cfg: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Resolve a curated ``(provider, model)`` pair to use when no instant
    model is configured (or the configured one is an unfixable thinking
    model) -- "pick a curated instant-capable default for whatever provider
    IS configured/reachable". Returns ``("", "")`` when nothing sane can be
    resolved; the caller treats that as "disable the instant lane"
    (:class:`InstantLaneUnavailable`)."""
    provider = _configured_main_provider(cfg)
    if not provider:
        return "", ""
    if _is_local_provider(cfg, provider):
        main_model = _configured_main_model(cfg)
        return (provider, main_model) if main_model else ("", "")
    curated = _curated_instant_model(provider)
    return (provider, curated) if curated else ("", "")


def resolve_instant_runtime(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resolve ``auxiliary.voice_instant.{provider,model,base_url,api_key}``
    into ``run_agent.AIAgent`` constructor kwargs.

    Mirrors ``agent/background_review.py``'s ``_resolve_review_runtime`` --
    same config namespace, same ``hermes_cli.runtime_provider.resolve_runtime_provider``
    credential resolution -- adapted for the instant lane, which (unlike the
    review fork) has no live parent ``AIAgent`` in this process to inherit a
    runtime from.

    Resolution order:

    1. ``auxiliary.voice_instant.{provider,model}`` both set (and provider
       not "auto") -> use them, UNLESS :func:`is_instant_capable` flags the
       model as a reasoning/thinking family, in which case
       :func:`thinking_off_params` is applied if the provider supports
       toggling reasoning off, else this falls through to (2) with a
       WARNING log naming the rejected model.
    2. Otherwise -> :func:`_resolve_fallback_instant_model` picks a curated
       fast default for the user's configured MAIN provider (never the
       normal auto-detect chain, which could resolve to the slow main
       model -- see the module docstring).

    Raises :class:`InstantLaneUnavailable` when neither step yields a usable
    (provider, model) pair -- "no sane fast default is resolvable" -> the
    instant lane is disabled for this turn (one clear warning logged here;
    the caller's single WS ``error`` event + escalation-path fallback is
    handled by ``hermes_cli.web_server``, see :class:`InstantLaneUnavailable`).
    """
    from hermes_cli.config import cfg_get, load_config

    cfg = cfg if cfg is not None else load_config()
    provider = str(cfg_get(cfg, "auxiliary", "voice_instant", "provider", default="") or "").strip().lower()
    model = str(cfg_get(cfg, "auxiliary", "voice_instant", "model", default="") or "").strip()
    base_url = str(cfg_get(cfg, "auxiliary", "voice_instant", "base_url", default="") or "").strip() or None
    api_key = str(cfg_get(cfg, "auxiliary", "voice_instant", "api_key", default="") or "").strip() or None

    explicit = bool(provider and provider != "auto" and model)
    reasoning_config: Optional[Dict[str, Any]] = None

    if not explicit:
        provider, model = _resolve_fallback_instant_model(cfg)
        base_url = None
        api_key = None
    elif not is_instant_capable(model):
        overrides = thinking_off_params(provider, model)
        if overrides:
            logger.info(
                "voice_instant_lane: configured instant model %s/%s looks like a "
                "reasoning/thinking model; disabling reasoning via reasoning_config "
                "to keep instant-lane latency low.",
                provider, model,
            )
            reasoning_config = overrides.get("reasoning_config")
        else:
            fallback_provider, fallback_model = _resolve_fallback_instant_model(cfg)
            logger.warning(
                "voice_instant_lane: configured instant model %s/%s is a "
                "reasoning/thinking model and this provider has no known way to "
                "disable reasoning -- the instant lane must never run on a slow "
                "thinking model (defeats the <1s voice-latency design). %s",
                provider, model,
                f"Falling back to {fallback_provider}/{fallback_model} for this turn."
                if fallback_model else
                "No curated fallback model is known for this provider either -- "
                "set auxiliary.voice_instant.model to a fast, non-thinking model.",
            )
            provider, model = fallback_provider, fallback_model
            base_url = None
            api_key = None

    if not provider or not model:
        logger.warning(
            "voice_instant_lane: no instant-capable model is configured or "
            "resolvable (auxiliary.voice_instant.{provider,model} is unset and no "
            "curated fast default is known for the configured main provider) -- "
            "disabling the instant lane for this turn; the utterance will route "
            "directly to the escalation deep-task path instead."
        )
        raise InstantLaneUnavailable(
            "voice instant lane: no instant-capable model is configured or "
            "resolvable -- set auxiliary.voice_instant.{provider,model} to a fast, "
            "non-thinking model to enable it"
        )

    # Hybrid models often reason by default without advertising it in their
    # name. Apply the voice-specific setting to every instant request.
    configured_reasoning = _resolve_instant_reasoning(cfg, provider)
    if configured_reasoning is not None:
        reasoning_config = configured_reasoning

    # Keep the voice lane on the same request-speed path as normal chat.  In
    # particular, `/fast`/Priority Processing is represented by service_tier
    # plus provider-specific request overrides in the regular chat setup.
    # Voice owns a separate AIAgent instance, so these values must be copied
    # explicitly or the two surfaces silently drift even when they use the
    # same provider and model.
    raw_service_tier = str(
        cfg_get(cfg, "auxiliary", "voice_instant", "service_tier", default="")
        or cfg_get(cfg, "agent", "service_tier", default="")
        or ""
    ).strip().lower()
    service_tier = (
        "priority" if raw_service_tier in {"fast", "priority", "on"} else None
    )
    request_overrides: Optional[Dict[str, Any]] = None
    if service_tier:
        try:
            from hermes_cli.models import resolve_fast_mode_overrides

            request_overrides = resolve_fast_mode_overrides(model)
        except Exception:
            logger.debug(
                "voice_instant_lane: fast-mode override resolution failed for model=%s",
                model,
                exc_info=True,
            )

    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        rp = resolve_runtime_provider(
            requested=provider,
            target_model=model,
            explicit_api_key=api_key,
            explicit_base_url=base_url,
        )
    except Exception:
        logger.debug(
            "voice_instant_lane: runtime resolution failed for provider=%s", provider, exc_info=True
        )
        return {
            "provider": provider,
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "api_mode": None,
            "reasoning_config": reasoning_config,
            "service_tier": service_tier,
            "request_overrides": request_overrides,
        }

    return {
        "provider": rp.get("provider") or provider,
        "model": model,
        "base_url": rp.get("base_url") or base_url,
        "api_key": rp.get("api_key") or api_key,
        "api_mode": rp.get("api_mode"),
        "reasoning_config": reasoning_config,
        "service_tier": service_tier,
        "request_overrides": request_overrides,
    }


# ---------------------------------------------------------------------------
# Streaming instant reply
# ---------------------------------------------------------------------------


def _build_deferred_context(cfg: Optional[Dict[str, Any]]) -> str:
    """Load bounded personal context after first audio starts."""
    from hermes_cli.config import cfg_get, load_config
    from tools.memory_tool import MemoryStore

    cfg = cfg if cfg is not None else load_config()
    blocks: List[str] = []
    durable_memory = ""
    memory_cfg = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
    memory_enabled = bool(memory_cfg.get("memory_enabled", True))
    user_enabled = bool(memory_cfg.get("user_profile_enabled", True))
    if memory_enabled or user_enabled:
        store = MemoryStore(
            memory_char_limit=int(memory_cfg.get("memory_char_limit", 2200) or 2200),
            user_char_limit=int(memory_cfg.get("user_char_limit", 1375) or 1375),
        )
        store.load_from_disk()
        if user_enabled:
            user = store.format_for_system_prompt("user")
            if user:
                blocks.append(user)
        if memory_enabled:
            memory = store.format_for_system_prompt("memory")
            if memory:
                durable_memory = memory

    # Metadata only: skill procedures require the deep lane, but names and
    # descriptions keep the instant model from feeling like a cold stranger.
    if bool(cfg_get(cfg, "skills", "enabled", default=True)):
        try:
            from agent.skill_commands import get_skill_commands

            lines: List[str] = []
            size = 0
            for item in get_skill_commands().values():
                name = str(item.get("name") or "").strip()
                description = str(item.get("description") or "").strip().replace("\n", " ")
                if not name:
                    continue
                line = f"- {name}: {description}" if description else f"- {name}"
                if size + len(line) + 1 > 800:
                    break
                lines.append(line)
                size += len(line) + 1
            if lines:
                blocks.append(
                    "Relevant skill index (awareness only; escalate when a skill procedure is needed):\n"
                    + "\n".join(lines)
                )
        except Exception:
            logger.debug("voice_instant_lane: deferred skill index failed", exc_info=True)
    if durable_memory:
        blocks.append(durable_memory)
    # World-awareness providers are read-only, bounded, ephemeral context.
    # This is the voice-lane counterpart to new gateway-session priming and
    # does not mutate the stable system prompt or conversation history.
    try:
        from hermes_cli.plugins import build_plugin_context_blocks, discover_plugins

        discover_plugins()
        blocks.extend(build_plugin_context_blocks())
    except Exception:
        logger.debug("voice_instant_lane: plugin world context failed", exc_info=True)

    # Smart-room ambient context (v0.3 spec §B.2): one compact room line,
    # config-gated by smart_room.context.enabled (default true). The
    # smart_room plugin is a bundled backend that ALSO registers this same
    # line as a generic context provider consumed by build_plugin_context_blocks()
    # just above -- that generic path has no config gate. This explicit,
    # gated call is belt-and-suspenders (mirrors the equivalent block in
    # gateway/run.py's session-priming path): it's the only way to actually
    # honor smart_room.context.enabled=false, and it's the sole source of
    # the line whenever plugin discovery didn't already deliver it. The
    # containment check keeps the two paths from ever double-appending the
    # identical line when both are active.
    try:
        if cfg_get(cfg, "smart_room", "context", "enabled", default=True):
            from plugins.smart_room.context import get_context_line

            _room_line = get_context_line()
            if _room_line and not any(_room_line in block for block in blocks):
                blocks.append(_room_line)
    except ImportError:
        pass
    except Exception:
        logger.debug("voice_instant_lane: smart-room context failed", exc_info=True)

    # Episodic memory hint (Loop 1, memory-maturity spec §1.3): a one-line
    # pointer so the instant lane reaches for recall_episode ("has this
    # happened before?") instead of assuming something is new. The tool
    # itself needs no whitelist entry here -- it's registered under the
    # "memory" toolset (tools/episodic_tool.py), the same toolset
    # recall_files/memory already flow through to this agent via its normal
    # configured toolset (see module docstring: "Tools: the user's normal
    # configured toolset"). Additive only, capped to a single short line.
    try:
        from agent.memory.episodic import episodic_config

        if episodic_config(cfg)["enabled"]:
            blocks.append(
                "Episodic memory is available: call recall_episode(query=...) to check "
                "whether something has happened before instead of assuming it's new."
            )
    except Exception:
        logger.debug("voice_instant_lane: episodic hint failed", exc_info=True)

    # Graph memory hint (graph-mind spec §2.4): same shape as the episodic
    # hint above -- recall_graph is registered under the "memory" toolset
    # (tools/graph_tool.py), so no whitelist entry is needed here either.
    # Additive only, capped to a single short line.
    try:
        from agent.memory.graph import graph_config

        if graph_config(cfg)["enabled"]:
            blocks.append(
                "Graph memory is available: call recall_graph(query=...) to see what's "
                "connected to a person, project, or topic before answering 'what's related "
                "to X' or 'why' questions from a flat guess."
            )
    except Exception:
        logger.debug("voice_instant_lane: graph hint failed", exc_info=True)

    return "\n\n".join(blocks)[:4500]


def _start_deferred_context_load(
    transcript: RollingTranscript, cfg: Optional[Dict[str, Any]],
) -> None:
    with transcript._deferred_context_lock:
        if transcript._deferred_context or transcript._deferred_context_loading:
            return
        transcript._deferred_context_loading = True

    def _load() -> None:
        start = time.monotonic()
        try:
            transcript._deferred_context = _build_deferred_context(cfg)
        except Exception:
            logger.debug("voice_instant_lane: deferred personal context failed", exc_info=True)
        finally:
            transcript._deferred_context_loading = False
            transcript._deferred_context_load_ms = (time.monotonic() - start) * 1000.0

    threading.Thread(target=_load, name="voice-instant-context", daemon=True).start()


def deferred_context_status(transcript: RollingTranscript) -> Tuple[str, Optional[float]]:
    """Snapshot of the deferred personal-context load for [VOICE-PERF]
    logging: ``("ready"|"loading"|"absent", load_ms)``.

    ``load_ms`` is only populated once a load has completed ("ready");
    ``None`` while still "loading" or if a load was never kicked off
    ("absent" -- e.g. memory/skills disabled in config, or this turn ran
    before :func:`_start_deferred_context_load`/:func:`warm_instant_lane`
    ever fired).
    """
    if transcript._deferred_context:
        return "ready", transcript._deferred_context_load_ms
    if transcript._deferred_context_loading:
        return "loading", None
    return "absent", None


def _runtime_key(runtime: Dict[str, Any], max_tokens: int) -> Tuple[Any, ...]:
    reasoning = runtime.get("reasoning_config") or {}
    request_overrides = runtime.get("request_overrides") or {}
    return (
        runtime.get("provider"), runtime.get("model"), runtime.get("base_url"),
        runtime.get("api_mode"), max_tokens, reasoning.get("enabled"), reasoning.get("effort"),
        runtime.get("service_tier"), tuple(sorted(request_overrides.items())),
    )


def _new_instant_agent(runtime: Dict[str, Any], max_tokens: int):
    from run_agent import AIAgent

    agent = AIAgent(
        provider=runtime["provider"],
        model=runtime["model"] or "",
        base_url=runtime["base_url"],
        api_key=runtime["api_key"],
        api_mode=runtime["api_mode"],
        max_tokens=max_tokens,
        reasoning_config=runtime.get("reasoning_config"),
        service_tier=runtime.get("service_tier"),
        request_overrides=runtime.get("request_overrides"),
        quiet_mode=True,
        platform="voice",
        skip_context_files=False,
        load_soul_identity=True,
        # The bounded deferred-context loader below already supplies durable
        # personal/world context before the first utterance. Starting the
        # external memory provider here duplicates that context and makes its
        # synchronous first-turn network prefetch (up to ~5s for Honcho) part
        # of voice TTFT. Keep the normal memory tools through the lazy canonical
        # store, but make external recall explicitly model-invoked.
        skip_memory=True,
    )
    agent._persist_disabled = True
    # Persistence remains disabled, but read-only recall tools may lazily open
    # their canonical stores.
    agent._recall_allowed_while_persist_disabled = True
    agent._memory_store = _LazyMemoryStore()
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent._memory_nudge_interval = 0
    agent._skill_nudge_interval = 0
    # Build the exact normal system prompt during session warm-up, before the
    # user speaks. Previously the first utterance spent several seconds here
    # (context files, tool guidance, goals and plugin context) before it even
    # opened the provider stream. This is still the ordinary byte-stable
    # AIAgent prompt -- only its timing moves off the spoken-turn critical path.
    build_prompt = getattr(agent, "_build_system_prompt", None)
    if callable(build_prompt) and not getattr(agent, "_cached_system_prompt", None):
        agent._cached_system_prompt = build_prompt()
    return agent


def warm_instant_lane(
    transcript: RollingTranscript, cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve + construct the instant-lane ``AIAgent`` and cache it onto
    ``transcript`` ahead of the first utterance, and kick the deferred
    personal-context load immediately instead of waiting for first audio.

    Meant to run on a background thread right at duplex session open (see
    ``hermes_cli.web_server._DuplexSession.start``), so the first real
    utterance finds both the agent AND the deferred context already warm
    (or well underway) instead of paying construction latency on the
    critical path of the user's first turn.

    This only PRE-POPULATES the same cache slot :func:`stream_instant_reply`
    already reads (``transcript._instant_agent`` / ``_instant_agent_key``)
    -- it never gates anything. If this hasn't finished (or failed) by the
    time the first utterance arrives, :func:`stream_instant_reply`'s own
    lazy construct-if-missing path is the fallback and behaves exactly as it
    did before this function existed.

    Returns timing/outcome info for the session-open [VOICE-PERF] line:
    ``{"ok": True, "construct_ms": float, "provider": str, "model": str}``
    on success, or ``{"ok": False, "error": str, "construct_ms": float}``
    on failure (e.g. :class:`InstantLaneUnavailable` -- logged here at
    WARNING and swallowed, since a failed warm-up must not crash session
    open; the first real utterance will simply hit the same failure again
    through the normal error-handling path).
    """
    start = time.monotonic()
    try:
        _load_learning_hints_once(transcript, cfg)
        runtime = resolve_instant_runtime(cfg)
        max_tokens = _resolve_instant_max_tokens(cfg)
        key = _runtime_key(runtime, max_tokens)
        agent = _new_instant_agent(runtime, max_tokens)
        construct_ms = (time.monotonic() - start) * 1000.0
        # Extremely unlikely this early in the session's life, but don't
        # clobber a slot something else already raced ahead and populated.
        if transcript._instant_agent is None:
            transcript._instant_agent = agent
            transcript._instant_agent_key = key
        _start_deferred_context_load(transcript, cfg)
        return {
            "ok": True,
            "construct_ms": construct_ms,
            "provider": runtime.get("provider"),
            "model": runtime.get("model"),
        }
    except Exception as exc:
        logger.warning("voice_instant_lane: session warm-up failed: %s", exc)
        return {"ok": False, "error": str(exc), "construct_ms": (time.monotonic() - start) * 1000.0}


def stream_instant_reply(
    transcript: RollingTranscript,
    utterance: str,
    *,
    allow_escalation: bool = True,
    cfg: Optional[Dict[str, Any]] = None,
    activity_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    warm_status_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Iterator[str]:
    """Run one fully tool-armed instant-lane agent turn and stream its text
    deltas.

    Reuses a per-duplex ``run_agent.AIAgent`` routed to
    ``auxiliary.voice_instant.*`` (see :func:`resolve_instant_runtime`). Its
    stable prompt is the normal Marvi system prompt; the voice addendum and
    bounded deferred personal context are ephemeral suffixes. Tool schemas are
    the same configured schemas visible to the normal agent.

    ``run_conversation`` is a single blocking call; its ``stream_callback``
    hook (the same one the existing voice-mode TTS pipeline uses to start
    audio before the full response is ready) fires with text deltas as the
    model streams them, INCLUDING through any tool-call turns -- not just a
    final synthetically-buffered turn. To expose that as a plain synchronous
    generator (this function's contract), the agent turn runs on its own
    worker thread and deltas are bridged back through a queue.

    Yields raw deltas -- NOT filtered for the ``[ESCALATE]`` marker; route
    them through :class:`EscalationStream` for that. Raises when the turn
    never produces a single delta (instant model/agent construction
    unreachable -- including :class:`InstantLaneUnavailable` propagating
    synchronously out of :func:`resolve_instant_runtime` before this
    generator's worker thread ever starts) so callers can implement the
    "instant model unreachable -> fall back to the main agent" behavior from
    the duplex spec. A failure AFTER at least one delta streamed is logged
    and swallowed -- the caller already has a partial answer to work with.

    ``warm_status_callback``, when given, fires exactly once per call
    (before any delta) with ``{"hit": bool, "construct_ms": float|None}`` --
    ``hit=True`` means :func:`warm_instant_lane` (or a prior turn) had
    already cached a matching agent, so this turn paid no construction
    latency; ``hit=False`` means this call had to construct one (or two, in
    the barge-in shadow-agent race below) and ``construct_ms`` is the total
    time spent doing so. Used by ``hermes_cli.web_server`` for
    [VOICE-PERF] per-turn logging.
    """
    runtime = resolve_instant_runtime(cfg)
    max_tokens = _resolve_instant_max_tokens(cfg)
    history = transcript.as_messages()
    if history and history[-1] == {"role": "user", "content": utterance.strip()}:
        history.pop()
    system_message = build_voice_mode_addendum(
        allow_escalation=allow_escalation,
        learning_hints=_load_learning_hints_once(transcript, cfg),
    )

    key = _runtime_key(runtime, max_tokens)
    cached_agent = transcript._instant_agent
    cache_hit = cached_agent is not None and transcript._instant_agent_key == key
    construct_ms: Optional[float] = None
    if not cache_hit:
        _construct_start = time.monotonic()
        cached_agent = _new_instant_agent(runtime, max_tokens)
        construct_ms = (time.monotonic() - _construct_start) * 1000.0
        transcript._instant_agent = cached_agent
        transcript._instant_agent_key = key

    # Barge-in can leave a cancelled provider call finishing in its worker.
    # Never share an AIAgent concurrently or make the new utterance wait.
    owns_cached_agent = transcript._instant_agent_lock.acquire(blocking=False)
    if owns_cached_agent:
        agent = cached_agent
    else:
        # A second, concurrent turn (barge-in race) needs its own shadow
        # agent -- counts as a miss too since it pays full construction cost.
        _shadow_start = time.monotonic()
        agent = _new_instant_agent(runtime, max_tokens)
        shadow_ms = (time.monotonic() - _shadow_start) * 1000.0
        cache_hit = False
        construct_ms = shadow_ms if construct_ms is None else construct_ms + shadow_ms

    if warm_status_callback:
        try:
            warm_status_callback({"hit": cache_hit, "construct_ms": construct_ms})
        except Exception:
            logger.debug("voice_instant_lane: warm_status_callback failed", exc_info=True)

    agent.ephemeral_system_prompt = system_message
    if _LOCAL_TIME_QUERY_RE.search(utterance):
        from hermes_time import now as hermes_now

        local_now = hermes_now()
        agent.ephemeral_system_prompt += (
            "\n\nCurrent local date and time: "
            f"{local_now.strftime('%A, %B %d, %Y at %H:%M:%S %Z%z')}. "
            "Use this exact value; do not estimate the time."
        )
    if transcript._deferred_context:
        agent.ephemeral_system_prompt += "\n\n" + transcript._deferred_context
    def _tool_activity(tool_name: str) -> Tuple[str, str]:
        return {
            "web_search": ("web", "Searching the web"),
            "web_extract": ("web", "Reading a web page"),
            "read_file": ("file", "Reading a file"),
            "search_files": ("file", "Searching files"),
            "memory": ("memory", "Updating memory"),
            "session_search": ("session", "Searching past conversations"),
            "terminal": ("delegation", "Running a command"),
            "process": ("delegation", "Running a process"),
            "execute_code": ("delegation", "Running code"),
            "delegate_task": ("delegation", "Delegating work"),
        }.get(tool_name, ("thinking", "Working on it"))

    def _tool_started(_call_id: str, tool_name: str, _args: Any) -> None:
        if activity_callback:
            if tool_name == "show_card" and isinstance(_args, dict):
                activity_callback({"status": "started", "kind": "card", "label": "Showing a card", "tool": tool_name, "card": _args})
                return
            kind, label = _tool_activity(tool_name)
            activity_callback({"status": "started", "kind": kind, "label": label, "tool": tool_name})

    def _tool_completed(_call_id: str, tool_name: str, _args: Any, _result: Any) -> None:
        if activity_callback:
            if tool_name == "show_card":
                return
            kind, label = _tool_activity(tool_name)
            activity_callback({"status": "completed", "kind": kind, "label": label, "tool": tool_name})

    agent.tool_start_callback = _tool_started
    agent.tool_complete_callback = _tool_completed

    delta_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    error_box: Dict[str, BaseException] = {}

    def _on_delta(text: str) -> None:
        if text:
            _start_deferred_context_load(transcript, cfg)
            delta_queue.put(text)

    def _worker() -> None:
        try:
            agent.run_conversation(
                utterance,
                conversation_history=history or None,
                stream_callback=_on_delta,
            )
        except BaseException as exc:  # noqa: BLE001 -- reraised on the caller's thread
            error_box["error"] = exc
        finally:
            if owns_cached_agent:
                transcript._instant_agent_lock.release()
            delta_queue.put(None)

    worker = threading.Thread(target=_worker, name="voice-instant-lane", daemon=True)
    worker.start()

    got_any_delta = False
    interrupted = False

    def _interrupt_cancelled_turn() -> None:
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        try:
            hard_interrupt = getattr(agent, "hard_interrupt", None)
            if callable(hard_interrupt):
                hard_interrupt("voice barge-in")
            else:
                interrupt = getattr(agent, "interrupt", None)
                if callable(interrupt):
                    interrupt("voice barge-in")
        except Exception:
            logger.debug("Voice instant lane: provider cancellation failed", exc_info=True)

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _interrupt_cancelled_turn()
                break
            try:
                item = delta_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                break
            if cancel_event is not None and cancel_event.is_set():
                _interrupt_cancelled_turn()
                break
            got_any_delta = True
            yield item
    finally:
        if cancel_event is not None and cancel_event.is_set():
            _interrupt_cancelled_turn()
    worker.join(timeout=5.0)

    if interrupted:
        return

    error = error_box.get("error")
    if error is not None:
        if not got_any_delta:
            raise error
        logger.warning("Voice instant lane: agent turn failed mid-reply: %s", error)


# ---------------------------------------------------------------------------
# Escalation marker parsing
# ---------------------------------------------------------------------------


@dataclass
class EscalationResult:
    escalate: bool
    text: str  # ack_text when escalate else the full reply text
    mode: Optional[str] = None  # "thinking" | "delegating"
    end_voice: bool = False

    @property
    def ack_text(self) -> Optional[str]:
        return self.text if self.escalate else None

    @property
    def reply_text(self) -> Optional[str]:
        return None if self.escalate else self.text


class EscalationStream:
    """Consumes text deltas from the instant model and resolves whether the
    accumulated reply is an ``[ESCALATE] <ack>`` hand-off.

    The marker can arrive split across multiple deltas (provider chunking is
    arbitrary), so resolution is deferred until either:

    - the buffered prefix stops matching ``[ESCALATE]`` character-for-character
      (resolved as an ordinary reply -- as early as the very first mismatching
      character, so a false/mid-text ``[ESCALATE]`` later in a normal reply
      never triggers this: only a marker at the very START of the response is
      ever considered), or
    - the buffer reaches the marker's full length while still matching
      (resolved as an escalation).

    Once resolved, every subsequent delta is classified immediately with no
    further buffering.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._resolved = False
        self._escalate = False
        self._end_voice = False
        self._mode: Optional[str] = None
        self.full_text = ""

    def feed(self, delta: str) -> Optional[str]:
        """Feed one raw delta.

        Returns the substring that is confirmed ordinary reply text (to
        forward to the caller as an ``instant_delta`` right now), or ``None``
        while resolution is still pending, or while resolved as an
        escalation (no reply deltas are ever surfaced for an escalating
        turn -- read the full ack text from :meth:`finish` instead).
        """
        if not delta:
            return None
        self.full_text += delta

        if self._resolved:
            return None if (self._escalate or self._end_voice) else delta

        self._buffer += delta
        markers = (
            (ESCALATE_MARKER, "thinking"),
            (DELEGATE_MARKER, "delegating"),
            (END_VOICE_MARKER, "end_voice"),
        )
        possible = [item for item in markers if item[0].startswith(self._buffer) or self._buffer.startswith(item[0])]
        if not possible:
            # Diverged from the marker -- definitely an ordinary reply.
            self._resolved = True
            self._escalate = False
            out = self._buffer
            self._buffer = ""
            return out

        matched = next((item for item in possible if self._buffer.startswith(item[0])), None)
        if matched is None:
            # Still an exact prefix match, but not enough characters yet.
            return None

        # Buffer length >= marker length and matches exactly -> escalation.
        self._resolved = True
        self._end_voice = matched[1] == "end_voice"
        self._escalate = not self._end_voice
        self._mode = None if self._end_voice else matched[1]
        self._buffer = ""
        return None

    def finish(self) -> EscalationResult:
        """Finalize after the underlying stream has ended.

        Handles the edge case of a reply that ends (or errors out) before
        enough characters arrived to definitively resolve -- e.g. the whole
        reply is exactly ``"[ESCALATE]"`` with nothing streamed after it.
        """
        if not self._resolved:
            if self._buffer == ESCALATE_MARKER:
                self._escalate = True
                self._mode = "thinking"
            elif self._buffer == DELEGATE_MARKER:
                self._escalate = True
                self._mode = "delegating"
            elif self._buffer == END_VOICE_MARKER:
                self._end_voice = True
            self._resolved = True

        if self._end_voice:
            goodbye = self.full_text[len(END_VOICE_MARKER):].strip()
            return EscalationResult(escalate=False, text=goodbye, end_voice=True)
        if self._escalate:
            marker = DELEGATE_MARKER if self._mode == "delegating" else ESCALATE_MARKER
            ack = self.full_text[len(marker):].strip()
            return EscalationResult(escalate=True, text=ack, mode=self._mode)
        return EscalationResult(escalate=False, text=self.full_text.strip())
