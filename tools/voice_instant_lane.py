"""Marvi's duplex-voice instant lane + escalation router.

Every finalized utterance in the ``/api/voice/duplex`` loop (see
``hermes_cli/web_server.py`` and
``docs/superpowers/specs/2026-07-10-marvi-duplex-voice-splitbrain-design.md``)
goes to a small, fast model first: the "instant lane". It gets the SAME full
system prompt the real agent gets (persona/SOUL.md, memory, skills,
environment -- the normal ``agent/prompt_builder.py`` pipeline, via a real
``run_agent.AIAgent``), with a voice-mode addendum appended at the end (never
interleaved into the stable/cacheable identity block -- see
``build_voice_mode_addendum``). That addendum instructs short spoken replies,
or -- when the ask needs more than a quick read/search or a tool outside its
whitelist -- to emit an ``[ESCALATE]`` marker instead, handing the turn to a
separate, fully tool-armed deep-task agent in the background
(``hermes_cli.web_server._duplex_run_deep_task``).

The instant lane is intentionally NOT a bare chat completion: it's a real,
tool-capable agent turn (``agent/background_review.py``'s fork pattern is the
closest existing model -- forks with a runtime, replays context, routes to an
auxiliary model), just capped hard for voice latency:

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
- **Tools**: a hard-coded, runtime-enforced whitelist
  (:data:`INSTANT_LANE_TOOL_WHITELIST`) -- fast reads only. Enforced via
  ``hermes_cli.plugins.set_thread_tool_whitelist``, the SAME mechanism
  ``agent/background_review.py``'s review fork uses, not just
  ``enabled_toolsets`` (defense in depth).
- **Iterations**: capped (:data:`INSTANT_LANE_MAX_ITERATIONS`) to roughly two
  tool calls before it must answer or escalate.

This module owns three things:

1. :func:`stream_instant_reply` -- runs that capped agent turn and streams
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
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

ESCALATE_MARKER = "[ESCALATE]"

DEFAULT_ROLLING_TURNS = 20
DEFAULT_MAX_TOKENS = 200

# Runtime-enforced tool whitelist for the instant lane -- fast reads only.
# Checked via hermes_cli.plugins.set_thread_tool_whitelist (the same
# mechanism agent/background_review.py's review fork uses), NOT just
# enabled_toolsets -- defense in depth regardless of what toolset-level tool
# definitions the model can see. Explicitly excludes write_file/patch (the
# rest of the "file" toolset) and the "memory" tool (read+write in one
# call) -- session_search is the read-only recall path instead.
INSTANT_LANE_TOOL_WHITELIST = frozenset(
    {
        "read_file",
        "search_files",  # file toolset, reads only -- no write_file/patch
        "web_search",
        "web_extract",  # web toolset -- search + light page fetch
        "session_search",  # read-only session/memory recall
    }
)
# Toolset-level gate for tool-definition generation (cache-prefix parity,
# mirrors background_review.py's enabled_toolsets usage). The whitelist
# above is the real enforcement boundary.
INSTANT_LANE_TOOLSETS = ["file", "web", "session_search"]
# ~2 tool calls (tool call -> result -> tool call -> result) then an answer.
INSTANT_LANE_MAX_ITERATIONS = 4
INSTANT_LANE_MAX_TOOL_CALLS = 2


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

    def add(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.turns.append({"role": role, "content": text})
        overflow = len(self.turns) - self.max_turns
        if overflow > 0:
            del self.turns[:overflow]

    def as_messages(self) -> List[Dict[str, str]]:
        return [dict(t) for t in self.turns]

    def clear(self) -> None:
        self.turns.clear()

    def __len__(self) -> int:
        return len(self.turns)


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
    "- You may use at most two quick tool calls (a file read, a web search, "
    "a memory recall) before answering. Only fast-read tools are available "
    "to you here -- no writing, no code execution, nothing heavy."
)

_ESCALATION_CONTRACT = (
    "\n\n"
    "Some asks need more than that -- more than two quick tool calls, a "
    "write/edit, code execution, or any tool you don't have access to here, "
    "multiple steps, or careful reasoning. When that's the case, do NOT "
    "attempt the answer yourself and do NOT keep trying tools. Instead "
    "reply with EXACTLY this and nothing else, on one line:\n\n"
    f"{ESCALATE_MARKER} <a short spoken acknowledgment, e.g. \"On it -- give "
    "me a minute to dig into that.\">\n\n"
    "The acknowledgment must be one short sentence, in your voice, said as "
    "if you're about to go look into it. Never write "
    f"{ESCALATE_MARKER} for anything you can actually answer in 1 to 3 "
    "sentences (with at most two quick tool calls) right now."
)


def build_voice_mode_addendum(*, allow_escalation: bool = True) -> str:
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
    return addendum


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
_INSTANT_DEFAULT_MODELS_SUPPLEMENTAL: Dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openai-codex": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "groq": "llama-3.1-8b-instant",
}

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
        }

    return {
        "provider": rp.get("provider") or provider,
        "model": model,
        "base_url": rp.get("base_url") or base_url,
        "api_key": rp.get("api_key") or api_key,
        "api_mode": rp.get("api_mode"),
        "reasoning_config": reasoning_config,
    }


# ---------------------------------------------------------------------------
# Streaming instant reply
# ---------------------------------------------------------------------------


def stream_instant_reply(
    transcript: RollingTranscript,
    utterance: str,
    *,
    allow_escalation: bool = True,
    cfg: Optional[Dict[str, Any]] = None,
) -> Iterator[str]:
    """Run one capped, tool-armed instant-lane agent turn and stream its text
    deltas.

    Builds a fresh ``run_agent.AIAgent`` (no ``ephemeral_system_prompt``, so
    the NORMAL full system-prompt pipeline runs -- persona/SOUL.md, memory,
    skills, environment hints, tool definitions, exactly like an ordinary
    top-level turn) routed to ``auxiliary.voice_instant.*`` (see
    :func:`resolve_instant_runtime`), with the voice-mode addendum appended
    via ``run_conversation``'s ``system_message`` and a runtime tool
    whitelist enforced via ``hermes_cli.plugins.set_thread_tool_whitelist``
    (:data:`INSTANT_LANE_TOOL_WHITELIST`) -- the same mechanism
    ``agent/background_review.py``'s review fork uses.

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
    """
    from hermes_cli.plugins import clear_thread_tool_whitelist, set_thread_tool_whitelist
    from run_agent import AIAgent

    runtime = resolve_instant_runtime(cfg)
    max_tokens = _resolve_instant_max_tokens(cfg)
    history = transcript.as_messages()
    system_message = build_voice_mode_addendum(allow_escalation=allow_escalation)

    agent = AIAgent(
        provider=runtime["provider"],
        model=runtime["model"] or "",
        base_url=runtime["base_url"],
        api_key=runtime["api_key"],
        api_mode=runtime["api_mode"],
        max_tokens=max_tokens,
        max_iterations=INSTANT_LANE_MAX_ITERATIONS,
        enabled_toolsets=INSTANT_LANE_TOOLSETS,
        reasoning_config=runtime.get("reasoning_config"),
        quiet_mode=True,
        platform="voice",
    )

    delta_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    error_box: Dict[str, BaseException] = {}

    def _on_delta(text: str) -> None:
        if text:
            delta_queue.put(text)

    def _worker() -> None:
        set_thread_tool_whitelist(
            set(INSTANT_LANE_TOOL_WHITELIST),
            deny_msg_fmt=(
                "Tool '{tool_name}' is not available in the voice instant "
                "lane (fast reads only). Say [ESCALATE] instead if you need it."
            ),
        )
        try:
            agent.run_conversation(
                utterance,
                system_message=system_message,
                conversation_history=history or None,
                stream_callback=_on_delta,
            )
        except BaseException as exc:  # noqa: BLE001 -- reraised on the caller's thread
            error_box["error"] = exc
        finally:
            clear_thread_tool_whitelist()
            delta_queue.put(None)

    worker = threading.Thread(target=_worker, name="voice-instant-lane", daemon=True)
    worker.start()

    got_any_delta = False
    while True:
        item = delta_queue.get()
        if item is None:
            break
        got_any_delta = True
        yield item
    worker.join(timeout=5.0)

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
            return None if self._escalate else delta

        self._buffer += delta
        marker_len = len(ESCALATE_MARKER)
        prefix_len = min(len(self._buffer), marker_len)
        if self._buffer[:prefix_len] != ESCALATE_MARKER[:prefix_len]:
            # Diverged from the marker -- definitely an ordinary reply.
            self._resolved = True
            self._escalate = False
            out = self._buffer
            self._buffer = ""
            return out

        if len(self._buffer) < marker_len:
            # Still an exact prefix match, but not enough characters yet.
            return None

        # Buffer length >= marker length and matches exactly -> escalation.
        self._resolved = True
        self._escalate = True
        self._buffer = ""
        return None

    def finish(self) -> EscalationResult:
        """Finalize after the underlying stream has ended.

        Handles the edge case of a reply that ends (or errors out) before
        enough characters arrived to definitively resolve -- e.g. the whole
        reply is exactly ``"[ESCALATE]"`` with nothing streamed after it.
        """
        if not self._resolved:
            self._escalate = self._buffer == ESCALATE_MARKER
            self._resolved = True

        if self._escalate:
            ack = self.full_text[len(ESCALATE_MARKER):].strip()
            return EscalationResult(escalate=True, text=ack)
        return EscalationResult(escalate=False, text=self.full_text.strip())
