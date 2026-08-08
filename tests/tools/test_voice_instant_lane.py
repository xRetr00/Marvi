"""Tests for tools/voice_instant_lane.py: escalation marker parsing, the
rolling transcript, the voice-mode addendum, config resolution, and the
instant-lane agent-turn streaming bridge.

No real model/network/tool access. ``run_agent.AIAgent`` is monkeypatched
with a small fake that mimics ``AIAgent.run_conversation``'s
``stream_callback`` contract (the same hook the existing voice-mode TTS
pipeline drives), so the queue-based streaming bridge and the tool-whitelist
enforcement are exercised without a real agent turn.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tools import voice_instant_lane as vil


class FakeInstantAgent:
    """Records constructor kwargs; ``run_conversation`` replays a canned
    sequence of stream_callback deltas (or raises)."""

    last_instance = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        FakeInstantAgent.last_instance = self

    def run_conversation(self, utterance, *, system_message=None, conversation_history=None, stream_callback=None):
        self.calls.append(
            {
                "utterance": utterance,
                "system_message": system_message,
                "conversation_history": conversation_history,
            }
        )
        for piece in getattr(self, "deltas", ["Hi", " there."]):
            if stream_callback:
                stream_callback(piece)
        if getattr(self, "raise_after", None) is not None:
            raise self.raise_after
        return {"final_response": "".join(getattr(self, "deltas", []))}


@pytest.fixture
def fake_agent_cls(monkeypatch):
    import run_agent

    monkeypatch.setattr(run_agent, "AIAgent", FakeInstantAgent)
    return FakeInstantAgent


# ---------------------------------------------------------------------------
# EscalationStream
# ---------------------------------------------------------------------------


class TestEscalationStream:
    def test_plain_reply_streams_through_unchanged(self):
        parser = vil.EscalationStream()
        out = []
        for delta in ["Hey", " there", "!"]:
            piece = parser.feed(delta)
            if piece:
                out.append(piece)
        result = parser.finish()

        assert "".join(out) == "Hey there!"
        assert result.escalate is False
        assert result.reply_text == "Hey there!"
        assert result.ack_text is None

    def test_marker_in_a_single_delta(self):
        parser = vil.EscalationStream()
        piece = parser.feed("[ESCALATE] On it, one sec.")
        result = parser.finish()

        assert piece is None
        assert result.escalate is True
        assert result.ack_text == "On it, one sec."
        assert result.reply_text is None
        assert result.mode == "thinking"

    def test_delegate_marker_routes_work_to_subagent(self):
        parser = vil.EscalationStream()
        parser.feed("[DELEGATE] I'll hand this to a sub-agent.")

        result = parser.finish()

        assert result.escalate is True
        assert result.mode == "delegating"
        assert result.ack_text == "I'll hand this to a sub-agent."

    def test_end_voice_marker_becomes_a_session_control_action(self):
        parser = vil.EscalationStream()
        pieces = [parser.feed(delta) for delta in ["[END_", "VOICE]", " Talk soon."]]

        result = parser.finish()

        assert pieces == [None, None, None]
        assert result.escalate is False
        assert result.end_voice is True
        assert result.reply_text == "Talk soon."

    def test_marker_split_across_many_deltas(self):
        parser = vil.EscalationStream()
        out = []
        for delta in ["[", "ESC", "ALATE", "]", " On it", " -- give me a sec."]:
            piece = parser.feed(delta)
            if piece:
                out.append(piece)
        result = parser.finish()

        assert out == []  # no reply deltas ever surfaced for an escalation
        assert result.escalate is True
        assert result.ack_text == "On it -- give me a sec."

    def test_marker_only_no_trailing_text(self):
        parser = vil.EscalationStream()
        piece = parser.feed("[ESCALATE]")
        result = parser.finish()

        assert piece is None
        assert result.escalate is True
        assert result.ack_text == ""

    def test_stream_ends_before_marker_resolves(self):
        """Reply is shorter than the marker and never matches it fully."""
        parser = vil.EscalationStream()
        piece = parser.feed("[ESC")
        assert piece is None  # still an exact prefix match, buffering
        result = parser.finish()

        assert result.escalate is False
        assert result.reply_text == "[ESC"

    def test_mid_text_false_marker_does_not_escalate(self):
        """A literal '[ESCALATE]' appearing mid-reply (not at the start)
        must NOT trigger escalation -- only a marker at position 0 counts."""
        parser = vil.EscalationStream()
        out = []
        for delta in [
            "I think you should try ",
            "[ESCALATE] as a search filter, ",
            "that should narrow it down.",
        ]:
            piece = parser.feed(delta)
            if piece:
                out.append(piece)
        result = parser.finish()

        assert result.escalate is False
        full = "".join(out)
        assert full == (
            "I think you should try [ESCALATE] as a search filter, "
            "that should narrow it down."
        )
        assert result.reply_text == full

    def test_diverges_on_first_character(self):
        parser = vil.EscalationStream()
        piece = parser.feed("Sure, here you go.")
        assert piece == "Sure, here you go."
        result = parser.finish()
        assert result.escalate is False

    def test_empty_delta_is_a_no_op(self):
        parser = vil.EscalationStream()
        assert parser.feed("") is None
        assert parser.feed(None) is None
        piece = parser.feed("Hi")
        assert piece == "Hi"


# ---------------------------------------------------------------------------
# RollingTranscript
# ---------------------------------------------------------------------------


class TestRollingTranscript:
    def test_add_and_as_messages(self):
        rt = vil.RollingTranscript(max_turns=20)
        rt.add("user", "hello")
        rt.add("assistant", "hi there")

        assert rt.as_messages() == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        assert len(rt) == 2

    def test_trims_to_max_turns(self):
        rt = vil.RollingTranscript(max_turns=3)
        for i in range(5):
            rt.add("user", f"msg{i}")

        assert [m["content"] for m in rt.as_messages()] == ["msg2", "msg3", "msg4"]

    def test_blank_text_is_ignored(self):
        rt = vil.RollingTranscript()
        rt.add("user", "   ")
        rt.add("user", "")
        assert len(rt) == 0

    def test_as_messages_returns_a_copy(self):
        rt = vil.RollingTranscript()
        rt.add("user", "hi")
        messages = rt.as_messages()
        messages[0]["content"] = "mutated"
        assert rt.as_messages()[0]["content"] == "hi"

    def test_clear(self):
        rt = vil.RollingTranscript()
        rt.add("user", "hi")
        rt.clear()
        assert len(rt) == 0

    def test_discard_last_only_removes_exact_pending_turn(self):
        rt = vil.RollingTranscript()
        rt.add("user", "first")
        rt.add("assistant", "reply")
        rt.add("user", "interrupted")

        assert rt.discard_last("user", "different") is False
        assert rt.discard_last("user", "interrupted") is True
        assert rt.as_messages()[-1] == {"role": "assistant", "content": "reply"}


# ---------------------------------------------------------------------------
# Voice-mode addendum
# ---------------------------------------------------------------------------


class TestVoiceModeAddendum:
    def test_always_present(self):
        addendum = vil.build_voice_mode_addendum(allow_escalation=False)
        assert "speaking out loud" in addendum
        assert "1 to 3 short" in addendum
        assert "markdown" in addendum.lower()

    def test_escalation_contract_only_when_allowed(self):
        with_escalation = vil.build_voice_mode_addendum(allow_escalation=True)
        without_escalation = vil.build_voice_mode_addendum(allow_escalation=False)

        assert vil.ESCALATE_MARKER in with_escalation
        assert vil.ESCALATE_MARKER not in without_escalation

    def test_exposes_full_configured_capabilities(self):
        addendum = vil.build_voice_mode_addendum(allow_escalation=True)
        assert "all tools enabled by the user's configuration" in addendum
        assert vil.END_VOICE_MARKER in addendum

    def test_learning_hints_are_loaded_once_per_session(self, monkeypatch):
        from agent.learning import escalation

        monkeypatch.setattr(escalation, "read_hints", lambda: "Escalate asks like: compare every option")
        transcript = vil.RollingTranscript()
        first = vil._load_learning_hints_once(transcript, {})
        monkeypatch.setattr(escalation, "read_hints", lambda: "changed mid-session")
        second = vil._load_learning_hints_once(transcript, {})

        assert first == second
        assert first in vil.build_voice_mode_addendum(allow_escalation=True, learning_hints=first)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestConfig:
    def test_escalation_enabled_defaults_true(self):
        assert vil.escalation_enabled({}) is True

    def test_escalation_enabled_respects_config(self):
        assert vil.escalation_enabled({"voice": {"escalation": {"enabled": False}}}) is False

    def test_resolve_instant_max_tokens_reads_auxiliary_voice_instant(self):
        cfg = {"auxiliary": {"voice_instant": {"max_tokens": 88}}}
        assert vil._resolve_instant_max_tokens(cfg) == 88

    def test_resolve_instant_max_tokens_defaults(self):
        assert vil._resolve_instant_max_tokens({}) == vil.DEFAULT_MAX_TOKENS

    def test_resolve_instant_runtime_raises_when_nothing_resolvable(self, monkeypatch):
        # No auxiliary.voice_instant.* configured AND no main provider
        # configured either -- the instant lane must NEVER silently fall
        # through to the normal auto-detect chain (which could resolve to
        # the slow main/thinking model). It must disable itself instead.
        monkeypatch.setattr(vil, "_configured_main_provider", lambda cfg: "")
        with pytest.raises(vil.InstantLaneUnavailable):
            vil.resolve_instant_runtime({})

    def test_resolve_instant_runtime_reads_auxiliary_voice_instant(self, monkeypatch):
        from hermes_cli import runtime_provider

        def fake_resolve_runtime_provider(*, requested, target_model, explicit_api_key, explicit_base_url):
            assert requested == "openrouter"
            assert target_model == "some/fast-model"
            return {"provider": "openrouter", "api_key": "resolved-key", "base_url": "https://x", "api_mode": None}

        monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", fake_resolve_runtime_provider)

        cfg = {"auxiliary": {"voice_instant": {"provider": "openrouter", "model": "some/fast-model"}}}
        runtime = vil.resolve_instant_runtime(cfg)

        assert runtime["provider"] == "openrouter"
        assert runtime["model"] == "some/fast-model"
        assert runtime["api_key"] == "resolved-key"
        assert runtime["reasoning_config"] == {"enabled": False, "effort": "none"}

    def test_instant_reasoning_can_be_enabled_independently(self, monkeypatch):
        from hermes_cli import runtime_provider

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            lambda **_kwargs: {
                "provider": "openrouter", "api_key": "key", "base_url": "https://x", "api_mode": None,
            },
        )
        cfg = {
            "auxiliary": {
                "voice_instant": {
                    "provider": "openrouter", "model": "some/fast-model", "reasoning_effort": "low",
                }
            }
        }

        assert vil.resolve_instant_runtime(cfg)["reasoning_config"] == {"enabled": True, "effort": "low"}

    def test_instant_runtime_inherits_normal_chat_fast_mode(self, monkeypatch):
        from hermes_cli import runtime_provider

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            lambda **_kwargs: {
                "provider": "openai", "api_key": "key", "base_url": "https://x", "api_mode": None,
            },
        )
        cfg = {
            "agent": {"service_tier": "fast"},
            "auxiliary": {"voice_instant": {"provider": "openai", "model": "gpt-5.4"}},
        }

        runtime = vil.resolve_instant_runtime(cfg)

        assert runtime["service_tier"] == "priority"
        assert runtime["request_overrides"] == {"service_tier": "priority"}

    def test_resolve_instant_runtime_treats_auto_as_unconfigured(self, monkeypatch):
        # provider="auto" is treated the same as "unset" -> falls back to a
        # curated instant model for the configured MAIN provider, never a
        # bare auto-detect that could land on the main/thinking model.
        from hermes_cli import runtime_provider

        monkeypatch.setattr(vil, "_configured_main_provider", lambda cfg: "anthropic")
        monkeypatch.setattr(
            runtime_provider, "resolve_runtime_provider",
            lambda **kw: {"provider": kw["requested"], "api_key": "k", "base_url": None, "api_mode": None},
        )
        cfg = {"auxiliary": {"voice_instant": {"provider": "auto"}}}
        runtime = vil.resolve_instant_runtime(cfg)
        assert runtime["provider"] == "anthropic"
        assert runtime["model"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# is_instant_capable / thinking_off_params -- the reasoning/"thinking"
# deny-list heuristic and the best-effort "turn reasoning off" params.
# ---------------------------------------------------------------------------


class TestIsInstantCapable:
    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5-20251001",
        "gpt-4o-mini",
        "gpt-5.4-mini",
        "gemini-3-flash-preview",
        "glm-4.5-flash",
        "llama-3.1-8b-instant",
        "llama-3.3-70b-versatile",
        "openai/gpt-4o-mini",
        "deepseek/deepseek-v4-flash",
        "kimi-k2-turbo-preview",
        "",
    ])
    def test_fast_models_are_capable(self, model):
        assert vil.is_instant_capable(model) is True

    @pytest.mark.parametrize("model", [
        "o1",
        "o1-mini",
        "o1-preview",
        "o3",
        "o3-mini",
        "o4-mini",
        "openai/o3",
        "deepseek-r1",
        "deepseek-r1-distill-llama-70b",
        "deepseek-reasoner",
        "gemini-2.5-flash-thinking",
        "grok-4-fast-reasoning",
        "qwq-32b",
        "glm-4.5-thinking",
    ])
    def test_thinking_models_are_not_capable(self, model):
        assert vil.is_instant_capable(model) is False

    def test_does_not_false_positive_on_substrings_that_merely_contain_digits(self):
        # "4o" (gpt-4o family) must not be confused with the "o1/o3/o4" deny
        # patterns, which require a boundary before/after the digit-letter pair.
        assert vil.is_instant_capable("gpt-4o-mini") is True
        assert vil.is_instant_capable("gpt-4o") is True


class TestThinkingOffParams:
    def test_known_toggle_provider_returns_reasoning_config(self):
        params = vil.thinking_off_params("anthropic", "claude-opus-4-6")
        assert params == {"reasoning_config": {"enabled": False, "effort": "none"}}

    def test_openai_returns_reasoning_config(self):
        params = vil.thinking_off_params("openai", "o3-mini")
        assert params["reasoning_config"]["enabled"] is False

    @pytest.mark.parametrize("provider", ["openrouter", "groq", "gemini", "vertex", "lmstudio"])
    def test_other_known_toggle_providers(self, provider):
        assert vil.thinking_off_params(provider, "some-thinking-model") != {}

    def test_unknown_provider_returns_empty(self):
        assert vil.thinking_off_params("some-exotic-provider", "o1") == {}

    def test_blank_provider_returns_empty(self):
        assert vil.thinking_off_params("", "o1") == {}


class TestResolveInstantRuntimeThinkingGuard:
    """resolve_instant_runtime() must never hand back a configured
    reasoning/thinking model as the instant model."""

    def test_configured_thinking_model_gets_reasoning_disabled(self, monkeypatch):
        from hermes_cli import runtime_provider

        monkeypatch.setattr(
            runtime_provider, "resolve_runtime_provider",
            lambda **kw: {"provider": kw["requested"], "api_key": "k", "base_url": None, "api_mode": None},
        )
        cfg = {"auxiliary": {"voice_instant": {"provider": "anthropic", "model": "claude-opus-4-6-thinking"}}}
        runtime = vil.resolve_instant_runtime(cfg)

        # Model is kept (Anthropic supports toggling reasoning off), but
        # reasoning_config disables it.
        assert runtime["provider"] == "anthropic"
        assert runtime["model"] == "claude-opus-4-6-thinking"
        assert runtime["reasoning_config"] == {"enabled": False, "effort": "none"}

    def test_configured_thinking_model_on_untoggleable_provider_falls_back(self, monkeypatch):
        from hermes_cli import runtime_provider

        monkeypatch.setattr(vil, "_curated_instant_model", lambda provider: "curated-fast-model")
        monkeypatch.setattr(vil, "_configured_main_provider", lambda cfg: "some-exotic-provider")
        monkeypatch.setattr(
            runtime_provider, "resolve_runtime_provider",
            lambda **kw: {"provider": kw["requested"], "api_key": "k", "base_url": None, "api_mode": None},
        )
        cfg = {"auxiliary": {"voice_instant": {"provider": "some-exotic-provider", "model": "o1-preview"}}}
        runtime = vil.resolve_instant_runtime(cfg)

        # The configured o1-preview model must NEVER be used -- falls back
        # to the curated model for the (fallback-resolved) main provider.
        assert runtime["model"] != "o1-preview"
        assert runtime["model"] == "curated-fast-model"
        assert runtime["reasoning_config"] is None

    def test_configured_thinking_model_with_no_fallback_raises(self, monkeypatch):
        monkeypatch.setattr(vil, "_curated_instant_model", lambda provider: "")
        monkeypatch.setattr(vil, "_configured_main_provider", lambda cfg: "")
        cfg = {"auxiliary": {"voice_instant": {"provider": "some-exotic-provider", "model": "o1-preview"}}}

        with pytest.raises(vil.InstantLaneUnavailable):
            vil.resolve_instant_runtime(cfg)


# ---------------------------------------------------------------------------
# Curated-default + local-provider fallback resolution helpers.
# ---------------------------------------------------------------------------


class TestCuratedInstantModel:
    def test_opencode_go_uses_voice_latency_override(self):
        assert vil._curated_instant_model("opencode-go") == "minimax-m2.5"

    def test_anthropic_resolves_via_aux_client_table(self):
        assert vil._curated_instant_model("anthropic") == "claude-haiku-4-5-20251001"

    def test_gemini_resolves_via_aux_client_table(self):
        assert vil._curated_instant_model("gemini")

    @pytest.mark.parametrize("provider,expected", [
        # Refreshed 2026-07-15 -- see _INSTANT_DEFAULT_MODELS_SUPPLEMENTAL's
        # docstring for why gpt-4o-mini/llama-3.1-8b-instant were replaced.
        ("openai", "gpt-5.4-mini"),
        ("openai-codex", "gpt-5.4-mini"),
        ("openrouter", "deepseek/deepseek-v4-flash"),
        ("groq", "llama-3.3-70b-versatile"),
    ])
    def test_supplemental_table_covers_providers_without_aux_profile(self, provider, expected):
        assert vil._curated_instant_model(provider) == expected

    def test_unknown_provider_returns_empty(self):
        assert vil._curated_instant_model("totally-unknown-provider-xyz") == ""

    def test_blank_provider_returns_empty(self):
        assert vil._curated_instant_model("") == ""


class TestConfiguredMainProvider:
    def test_reads_model_provider_from_config(self):
        cfg = {"model": {"provider": "openai"}}
        assert vil._configured_main_provider(cfg) == "openai"

    def test_auto_is_treated_as_unset(self):
        cfg = {"model": {"provider": "auto"}}
        assert vil._configured_main_provider(cfg) == ""

    def test_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "groq")
        assert vil._configured_main_provider({}) == "groq"

    def test_nothing_configured_returns_empty(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
        assert vil._configured_main_provider({}) == ""


class TestIsLocalProvider:
    @pytest.mark.parametrize("provider", ["ollama", "llamacpp", "llama-cpp", "llama.cpp", "vllm", "lmstudio", "local"])
    def test_known_local_provider_names(self, provider):
        assert vil._is_local_provider({}, provider) is True

    def test_localhost_base_url_is_local(self):
        cfg = {"model": {"base_url": "http://localhost:8080/v1"}}
        assert vil._is_local_provider(cfg, "custom") is True

    def test_loopback_ip_base_url_is_local(self):
        cfg = {"model": {"base_url": "http://127.0.0.1:1234/v1"}}
        assert vil._is_local_provider(cfg, "custom") is True

    def test_hosted_provider_is_not_local(self):
        cfg = {"model": {"base_url": "https://api.openai.com/v1"}}
        assert vil._is_local_provider(cfg, "openai") is False


class TestResolveFallbackInstantModel:
    def test_local_provider_reuses_configured_main_model_as_is(self, monkeypatch):
        cfg = {"model": {"provider": "ollama", "default": "my-local-gguf:latest"}}
        provider, model = vil._resolve_fallback_instant_model(cfg)
        assert provider == "ollama"
        assert model == "my-local-gguf:latest"

    def test_local_provider_with_no_model_configured_is_unresolvable(self):
        cfg = {"model": {"provider": "ollama"}}
        provider, model = vil._resolve_fallback_instant_model(cfg)
        assert (provider, model) == ("", "")

    def test_hosted_provider_uses_curated_model(self):
        cfg = {"model": {"provider": "anthropic"}}
        provider, model = vil._resolve_fallback_instant_model(cfg)
        assert provider == "anthropic"
        assert model == "claude-haiku-4-5-20251001"

    def test_no_provider_configured_is_unresolvable(self, monkeypatch):
        monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
        assert vil._resolve_fallback_instant_model({}) == ("", "")

    def test_provider_with_no_curated_default_is_unresolvable(self):
        cfg = {"model": {"provider": "totally-unknown-provider-xyz"}}
        assert vil._resolve_fallback_instant_model(cfg) == ("", "")


# ---------------------------------------------------------------------------
# stream_instant_reply
# ---------------------------------------------------------------------------


class TestStreamInstantReply:
    """These tests exercise the streaming/threading/tool-whitelist bridge,
    not instant-model resolution -- resolve_instant_runtime() is stubbed out
    so cfg={} keeps working regardless of what provider/model resolution
    would otherwise pick (that behavior has its own dedicated tests above)."""

    @pytest.fixture(autouse=True)
    def _stub_runtime(self, monkeypatch):
        monkeypatch.setattr(
            vil,
            "resolve_instant_runtime",
            lambda cfg=None: {
                "provider": "test-provider",
                "model": "test-model",
                "base_url": None,
                "api_key": None,
                "api_mode": None,
                "reasoning_config": None,
            },
        )

    def test_yields_text_deltas_from_stream_callback(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        transcript.add("user", "earlier turn")

        deltas = list(vil.stream_instant_reply(transcript, "hi there", cfg={}))

        assert deltas == ["Hi", " there."]
        call = fake_agent_cls.last_instance.calls[0]
        assert call["utterance"] == "hi there"
        assert call["conversation_history"] == [{"role": "user", "content": "earlier turn"}]
        assert "speaking out loud" in fake_agent_cls.last_instance.ephemeral_system_prompt

    def test_current_utterance_is_not_duplicated_in_history(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        transcript.add("user", "earlier")
        transcript.add("assistant", "reply")
        transcript.add("user", "current")

        list(vil.stream_instant_reply(transcript, "current", cfg={}))

        call = fake_agent_cls.last_instance.calls[0]
        assert call["conversation_history"] == [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ]

    def test_constructs_agent_with_full_prompt_and_default_toolset(self, fake_agent_cls):
        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

        kwargs = fake_agent_cls.last_instance.kwargs
        assert "enabled_toolsets" not in kwargs
        assert "allowed_tool_names" not in kwargs
        assert "max_iterations" not in kwargs
        assert kwargs.get("ephemeral_system_prompt") in (None, "")
        assert kwargs["skip_context_files"] is False
        assert kwargs["load_soul_identity"] is True
        assert kwargs["skip_memory"] is False
        assert fake_agent_cls.last_instance._memory_nudge_interval == 0
        assert fake_agent_cls.last_instance._skill_nudge_interval == 0
        assert not hasattr(fake_agent_cls.last_instance, "_cached_system_prompt")
        assert fake_agent_cls.last_instance._recall_allowed_while_persist_disabled is True

    def test_constructs_agent_with_fast_request_settings(self, monkeypatch, fake_agent_cls):
        monkeypatch.setattr(
            vil,
            "resolve_instant_runtime",
            lambda cfg=None: {
                "provider": "openai",
                "model": "gpt-5.4",
                "base_url": None,
                "api_key": "key",
                "api_mode": None,
                "reasoning_config": {"enabled": False, "effort": "none"},
                "service_tier": "priority",
                "request_overrides": {"service_tier": "priority"},
            },
        )

        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

        kwargs = fake_agent_cls.last_instance.kwargs
        assert kwargs["service_tier"] == "priority"
        assert kwargs["request_overrides"] == {"service_tier": "priority"}

    def test_reuses_warm_agent_and_adds_deferred_context(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        list(vil.stream_instant_reply(transcript, "hi", cfg={}))
        agent = fake_agent_cls.last_instance
        transcript._deferred_context = "User likes concise voice replies."

        list(vil.stream_instant_reply(transcript, "again", cfg={}))

        assert fake_agent_cls.last_instance is agent
        assert len(agent.calls) == 2
        assert "User likes concise voice replies." in agent.ephemeral_system_prompt

    def test_time_question_gets_exact_local_clock_only_for_that_turn(self, fake_agent_cls, monkeypatch):
        import hermes_time

        local_now = datetime(2026, 7, 17, 18, 42, 9, tzinfo=ZoneInfo("Europe/Istanbul"))
        monkeypatch.setattr(hermes_time, "now", lambda: local_now)
        transcript = vil.RollingTranscript()

        list(vil.stream_instant_reply(transcript, "What time is it?", cfg={}))
        agent = fake_agent_cls.last_instance
        assert "Friday, July 17, 2026 at 18:42:09" in agent.ephemeral_system_prompt
        assert "do not estimate the time" in agent.ephemeral_system_prompt

        list(vil.stream_instant_reply(transcript, "Hello", cfg={}))
        assert "Current local date and time" not in agent.ephemeral_system_prompt

    def test_no_conversation_history_when_transcript_empty(self, fake_agent_cls):
        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))
        call = fake_agent_cls.last_instance.calls[0]
        assert call["conversation_history"] is None

    def test_raises_when_agent_construction_fails_immediately(self, monkeypatch):
        import run_agent

        class BoomAgent:
            def __init__(self, **kwargs):
                raise RuntimeError("no provider configured")

        monkeypatch.setattr(run_agent, "AIAgent", BoomAgent)

        with pytest.raises(RuntimeError):
            list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

    def test_raises_when_turn_fails_before_any_delta(self, monkeypatch):
        import run_agent

        class FailFastAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, *a, **k):
                raise RuntimeError("provider unreachable")

        monkeypatch.setattr(run_agent, "AIAgent", FailFastAgent)

        with pytest.raises(RuntimeError):
            list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

    def test_swallows_error_after_partial_reply(self, monkeypatch):
        import run_agent

        class MidFailAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, utterance, *, system_message=None, conversation_history=None, stream_callback=None):
                stream_callback("partial")
                raise RuntimeError("boom mid stream")

        monkeypatch.setattr(run_agent, "AIAgent", MidFailAgent)

        deltas = list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))
        assert deltas == ["partial"]

    def test_does_not_install_a_thread_tool_whitelist(self, monkeypatch):
        import run_agent
        from hermes_cli import plugins

        seen = {}

        class WhitelistCheckAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, utterance, *, system_message=None, conversation_history=None, stream_callback=None):
                seen["allowed"] = getattr(plugins._thread_tool_whitelist, "allowed", "MISSING")
                stream_callback("ok")

        monkeypatch.setattr(run_agent, "AIAgent", WhitelistCheckAgent)

        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

        assert seen["allowed"] in (None, "MISSING")

    def test_reports_tool_activity_for_voice_cues(self, monkeypatch):
        import run_agent

        class ToolAgent:
            def __init__(self, **_kwargs):
                pass

            def run_conversation(self, _utterance, *, conversation_history=None, stream_callback=None):
                self.tool_start_callback("call-1", "web_search", {"query": "weather"})
                self.tool_complete_callback("call-1", "web_search", {}, "sunny")
                stream_callback("It is sunny.")

        monkeypatch.setattr(run_agent, "AIAgent", ToolAgent)
        activity = []

        list(vil.stream_instant_reply(vil.RollingTranscript(), "weather", cfg={}, activity_callback=activity.append))

        assert activity == [
            {"status": "started", "kind": "web", "label": "Searching the web", "tool": "web_search"},
            {"status": "completed", "kind": "web", "label": "Searching the web", "tool": "web_search"},
        ]

    def test_forwards_show_card_to_the_duplex_presentation(self, monkeypatch):
        import run_agent

        card = {"title": "Weather", "body": "Sunny, 25°C", "kind": "result"}

        class CardAgent:
            def __init__(self, **_kwargs):
                pass

            def run_conversation(self, _utterance, *, conversation_history=None, stream_callback=None):
                self.tool_start_callback("call-1", "show_card", card)
                self.tool_complete_callback("call-1", "show_card", card, {"success": True})
                stream_callback("It is sunny.")

        monkeypatch.setattr(run_agent, "AIAgent", CardAgent)
        activity = []

        list(vil.stream_instant_reply(vil.RollingTranscript(), "weather", cfg={}, activity_callback=activity.append))

        assert activity == [
            {"status": "started", "kind": "card", "label": "Showing a card", "tool": "show_card", "card": card}
        ]

    def test_memory_store_stays_lazy_until_memory_is_used(self):
        store = vil._LazyMemoryStore()
        store.reset_consolidation_failures()
        assert store._store is None

        _ = store.memory_entries
        assert store._store is not None

    def test_clears_tool_whitelist_after_the_turn(self, monkeypatch):
        import run_agent
        from hermes_cli import plugins

        events = threading.Event()
        after = {}

        class WhitelistAgent:
            def __init__(self, **kwargs):
                pass

            def run_conversation(self, utterance, *, system_message=None, conversation_history=None, stream_callback=None):
                stream_callback("ok")

        monkeypatch.setattr(run_agent, "AIAgent", WhitelistAgent)

        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))
        # The whitelist is thread-local to the agent's own worker thread; the
        # calling (test) thread must never have it set.
        assert getattr(plugins._thread_tool_whitelist, "allowed", None) is None

    def test_warm_status_callback_reports_hit_on_pre_warmed_agent(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        list(vil.stream_instant_reply(transcript, "hi", cfg={}))  # first turn: cold miss, warms the cache

        statuses = []
        list(
            vil.stream_instant_reply(
                transcript, "again", cfg={}, warm_status_callback=statuses.append,
            )
        )

        assert statuses == [{"hit": True, "construct_ms": None}]

    def test_warm_status_callback_reports_miss_with_construct_ms_when_cold(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        statuses = []

        list(
            vil.stream_instant_reply(
                transcript, "hi", cfg={}, warm_status_callback=statuses.append,
            )
        )

        assert len(statuses) == 1
        assert statuses[0]["hit"] is False
        assert isinstance(statuses[0]["construct_ms"], float)
        assert statuses[0]["construct_ms"] >= 0.0

    def test_warm_status_callback_failure_does_not_break_the_turn(self, monkeypatch, fake_agent_cls):
        def boom(_status):
            raise RuntimeError("callback exploded")

        deltas = list(
            vil.stream_instant_reply(
                vil.RollingTranscript(), "hi", cfg={}, warm_status_callback=boom,
            )
        )
        assert deltas == ["Hi", " there."]


# ---------------------------------------------------------------------------
# warm_instant_lane -- session-open pre-warm (called from
# hermes_cli.web_server._DuplexSession.start on WS connect).
# ---------------------------------------------------------------------------


class TestWarmInstantLane:
    @pytest.fixture(autouse=True)
    def _stub_runtime(self, monkeypatch):
        monkeypatch.setattr(
            vil,
            "resolve_instant_runtime",
            lambda cfg=None: {
                "provider": "test-provider",
                "model": "test-model",
                "base_url": None,
                "api_key": None,
                "api_mode": None,
                "reasoning_config": None,
            },
        )

    def test_constructs_and_caches_the_agent(self, fake_agent_cls):
        transcript = vil.RollingTranscript()

        result = vil.warm_instant_lane(transcript, cfg={})

        assert result["ok"] is True
        assert isinstance(result["construct_ms"], float)
        assert result["provider"] == "test-provider"
        assert result["model"] == "test-model"
        assert transcript._instant_agent is fake_agent_cls.last_instance
        assert transcript._instant_agent_key is not None

    def test_kicks_off_the_deferred_context_load(self, fake_agent_cls, monkeypatch):
        started = threading.Event()

        def fake_build(cfg):
            started.set()
            return "some context"

        monkeypatch.setattr(vil, "_build_deferred_context", fake_build)

        transcript = vil.RollingTranscript()
        vil.warm_instant_lane(transcript, cfg={})

        assert started.wait(timeout=2.0)

    def test_first_turn_after_warmup_reuses_the_warmed_agent(self, fake_agent_cls):
        transcript = vil.RollingTranscript()
        vil.warm_instant_lane(transcript, cfg={})
        warmed_agent = fake_agent_cls.last_instance

        statuses = []
        deltas = list(
            vil.stream_instant_reply(
                transcript, "hello", cfg={}, warm_status_callback=statuses.append,
            )
        )

        assert deltas == ["Hi", " there."]
        assert fake_agent_cls.last_instance is warmed_agent
        assert statuses == [{"hit": True, "construct_ms": None}]

    def test_does_not_clobber_an_agent_already_cached(self, fake_agent_cls):
        """Extremely unlikely race (something else warmed the slot first),
        but warm_instant_lane must never stomp on it."""
        transcript = vil.RollingTranscript()
        sentinel = object()
        transcript._instant_agent = sentinel
        transcript._instant_agent_key = ("already", "warm")

        vil.warm_instant_lane(transcript, cfg={})

        assert transcript._instant_agent is sentinel

    def test_failure_is_caught_and_reported_not_raised(self, monkeypatch):
        def boom(cfg=None):
            raise vil.InstantLaneUnavailable("no instant model configured")

        monkeypatch.setattr(vil, "resolve_instant_runtime", boom)
        transcript = vil.RollingTranscript()

        result = vil.warm_instant_lane(transcript, cfg={})

        assert result["ok"] is False
        assert "no instant model configured" in result["error"]
        assert transcript._instant_agent is None


# ---------------------------------------------------------------------------
# _build_deferred_context -- smart-room ambient context block
# ---------------------------------------------------------------------------


class TestDeferredContextSmartRoom:
    """The smart-room ambient-context block in ``_build_deferred_context``
    (v0.3 spec §B.2): config-gated, belt-and-suspenders with the generic
    plugin-context-provider path (build_plugin_context_blocks), and never
    duplicated. Uses a fake ``plugins.smart_room.context`` module -- never
    imports the real plugin/runtime.
    """

    def _minimal_cfg(self, **smart_room_context):
        cfg = {
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "skills": {"enabled": False},
        }
        if smart_room_context:
            cfg["smart_room"] = {"context": smart_room_context}
        return cfg

    def _fake_smart_room_context(self, monkeypatch, get_context_line):
        fake_pkg = types.ModuleType("plugins.smart_room")
        fake_context = types.ModuleType("plugins.smart_room.context")
        fake_context.get_context_line = get_context_line
        monkeypatch.setitem(sys.modules, "plugins.smart_room", fake_pkg)
        monkeypatch.setitem(sys.modules, "plugins.smart_room.context", fake_context)

    def _no_generic_plugin_context(self, monkeypatch, blocks=None):
        import hermes_cli.plugins as plugins_mod

        monkeypatch.setattr(plugins_mod, "discover_plugins", lambda: None)
        monkeypatch.setattr(plugins_mod, "build_plugin_context_blocks", lambda: list(blocks or []))

    def test_room_line_included_when_enabled(self, monkeypatch):
        self._no_generic_plugin_context(monkeypatch)
        self._fake_smart_room_context(monkeypatch, lambda: "Room: reading mode, Shereef present.")

        result = vil._build_deferred_context(self._minimal_cfg(enabled=True))

        assert "Room: reading mode, Shereef present." in result

    def test_room_line_included_by_default_when_unconfigured(self, monkeypatch):
        # smart_room.context.enabled defaults to True when absent from config.
        self._no_generic_plugin_context(monkeypatch)
        self._fake_smart_room_context(monkeypatch, lambda: "Room: light off, phone: home.")

        result = vil._build_deferred_context(self._minimal_cfg())

        assert "Room: light off, phone: home." in result

    def test_room_line_omitted_when_config_disabled(self, monkeypatch):
        self._no_generic_plugin_context(monkeypatch)
        self._fake_smart_room_context(monkeypatch, lambda: "Room: reading mode, Shereef present.")

        result = vil._build_deferred_context(self._minimal_cfg(enabled=False))

        assert "Room:" not in result

    def test_no_line_returned_is_a_silent_noop(self, monkeypatch):
        self._no_generic_plugin_context(monkeypatch)
        self._fake_smart_room_context(monkeypatch, lambda: None)

        result = vil._build_deferred_context(self._minimal_cfg(enabled=True))

        assert "Room:" not in result

    def test_missing_plugin_is_silently_ignored(self, monkeypatch):
        self._no_generic_plugin_context(monkeypatch)
        monkeypatch.setitem(sys.modules, "plugins.smart_room", None)
        monkeypatch.setitem(sys.modules, "plugins.smart_room.context", None)

        result = vil._build_deferred_context(self._minimal_cfg(enabled=True))

        assert "Room:" not in result

    def test_get_context_line_exception_is_swallowed(self, monkeypatch):
        self._no_generic_plugin_context(monkeypatch)

        def _boom():
            raise RuntimeError("bridge unreachable")

        self._fake_smart_room_context(monkeypatch, _boom)

        result = vil._build_deferred_context(self._minimal_cfg(enabled=True))

        assert "Room:" not in result

    def test_not_duplicated_when_generic_plugin_path_already_delivered_it(self, monkeypatch):
        # build_plugin_context_blocks() (the generic path) already produced
        # the identical line -- the dedicated call must not add it twice.
        line = "Room: reading mode, Shereef present."
        self._no_generic_plugin_context(monkeypatch, blocks=[line])
        self._fake_smart_room_context(monkeypatch, lambda: line)

        result = vil._build_deferred_context(self._minimal_cfg(enabled=True))

        assert result.count(line) == 1


# ---------------------------------------------------------------------------
# deferred_context_status
# ---------------------------------------------------------------------------


class TestDeferredContextStatus:
    def test_absent_before_any_load_starts(self):
        transcript = vil.RollingTranscript()
        assert vil.deferred_context_status(transcript) == ("absent", None)

    def test_loading_while_in_flight(self):
        transcript = vil.RollingTranscript()
        transcript._deferred_context_loading = True
        assert vil.deferred_context_status(transcript) == ("loading", None)

    def test_ready_with_load_ms_once_complete(self):
        transcript = vil.RollingTranscript()
        transcript._deferred_context = "some context"
        transcript._deferred_context_load_ms = 42.5
        assert vil.deferred_context_status(transcript) == ("ready", 42.5)

    def test_start_deferred_context_load_populates_load_ms(self, monkeypatch):
        monkeypatch.setattr(vil, "_build_deferred_context", lambda cfg: "built context")
        transcript = vil.RollingTranscript()

        vil._start_deferred_context_load(transcript, {})

        # Poll briefly for the background thread to finish.
        start = time.monotonic()
        while time.monotonic() - start < 2.0 and transcript._deferred_context_loading:
            time.sleep(0.01)

        status, load_ms = vil.deferred_context_status(transcript)
        assert status == "ready"
        assert load_ms is not None and load_ms >= 0.0
