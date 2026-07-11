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

import threading

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

    def test_mentions_tool_call_cap(self):
        addendum = vil.build_voice_mode_addendum(allow_escalation=True)
        assert "two quick tool calls" in addendum


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
        assert runtime["reasoning_config"] is None

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
        "gemini-3-flash-preview",
        "glm-4.5-flash",
        "llama-3.1-8b-instant",
        "openai/gpt-4o-mini",
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
    def test_anthropic_resolves_via_aux_client_table(self):
        assert vil._curated_instant_model("anthropic") == "claude-haiku-4-5-20251001"

    def test_gemini_resolves_via_aux_client_table(self):
        assert vil._curated_instant_model("gemini")

    @pytest.mark.parametrize("provider,expected", [
        ("openai", "gpt-4o-mini"),
        ("openai-codex", "gpt-4o-mini"),
        ("openrouter", "openai/gpt-4o-mini"),
        ("groq", "llama-3.1-8b-instant"),
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
        assert "speaking out loud" in call["system_message"]

    def test_constructs_agent_with_capped_toolsets_and_iterations(self, fake_agent_cls):
        list(vil.stream_instant_reply(vil.RollingTranscript(), "hi", cfg={}))

        kwargs = fake_agent_cls.last_instance.kwargs
        assert kwargs["enabled_toolsets"] == vil.INSTANT_LANE_TOOLSETS
        assert kwargs["max_iterations"] == vil.INSTANT_LANE_MAX_ITERATIONS
        assert kwargs.get("ephemeral_system_prompt") in (None, "")

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

    def test_enforces_tool_whitelist_during_the_turn(self, monkeypatch):
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

        assert seen["allowed"] == set(vil.INSTANT_LANE_TOOL_WHITELIST)
        assert "write_file" not in seen["allowed"]
        assert "memory" not in seen["allowed"]
        assert "terminal" not in seen["allowed"]

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
