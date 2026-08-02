"""Tests for setup.py configuration flows."""
import sys
import os
import json
import types

from hermes_cli.config import load_config, save_config
from hermes_cli import setup as setup_mod
from hermes_cli.setup import setup_model_provider


def _maybe_keep_current_tts(question, choices):
    if question != "Select TTS provider:":
        return None
    assert choices[-1].startswith("Keep current (")
    return len(choices) - 1


def _clear_provider_env(monkeypatch):
    for key in (
        "NOUS_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "LLM_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _clear_vercel_env(monkeypatch):
    for key in (
        "TERMINAL_VERCEL_RUNTIME",
        "VERCEL_OIDC_TOKEN",
        "VERCEL_TOKEN",
        "VERCEL_PROJECT_ID",
        "VERCEL_TEAM_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def _stub_tts(monkeypatch):
    """Stub out TTS prompts so setup_model_provider doesn't block."""
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda q, c, d=0: (
        _maybe_keep_current_tts(q, c) if _maybe_keep_current_tts(q, c) is not None
        else d
    ))
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda *a, **kw: False)


def _write_model_config(tmp_path, provider, base_url="", model_name="test-model"):
    """Simulate what a _model_flow_* function writes to disk."""
    cfg = load_config()
    m = cfg.get("model")
    if not isinstance(m, dict):
        m = {"default": m} if m else {}
        cfg["model"] = m
    m["provider"] = provider
    if base_url:
        m["base_url"] = base_url
    if model_name:
        m["default"] = model_name
    save_config(cfg)


def test_setup_delegates_to_select_provider_and_model(tmp_path, monkeypatch):
    """setup_model_provider calls select_provider_and_model and syncs config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()

    def fake_select():
        _write_model_config(tmp_path, "custom", "http://localhost:11434/v1", "qwen3.5:32b")

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert isinstance(reloaded["model"], dict)
    assert reloaded["model"]["provider"] == "custom"
    assert reloaded["model"]["base_url"] == "http://localhost:11434/v1"
    assert reloaded["model"]["default"] == "qwen3.5:32b"


def test_setup_syncs_openrouter_from_disk(tmp_path, monkeypatch):
    """When select_provider_and_model saves OpenRouter config to disk,
    the wizard's config dict picks it up."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()
    assert isinstance(config.get("model"), str)  # fresh install

    def fake_select():
        _write_model_config(tmp_path, "openrouter", model_name="anthropic/claude-opus-4.6")

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert isinstance(reloaded["model"], dict)
    assert reloaded["model"]["provider"] == "openrouter"


def test_setup_syncs_nous_from_disk(tmp_path, monkeypatch):
    """Nous OAuth writes config to disk; wizard config dict must pick it up."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()

    def fake_select():
        _write_model_config(tmp_path, "nous", "https://inference.example.com/v1", "gemini-3-flash")

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert isinstance(reloaded["model"], dict)
    assert reloaded["model"]["provider"] == "nous"
    assert reloaded["model"]["base_url"] == "https://inference.example.com/v1"


def test_setup_custom_providers_synced(tmp_path, monkeypatch):
    """custom_providers written by select_provider_and_model must survive."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()

    def fake_select():
        _write_model_config(tmp_path, "custom", "http://localhost:8080/v1", "llama3")
        cfg = load_config()
        cfg["custom_providers"] = [{"name": "Local", "base_url": "http://localhost:8080/v1"}]
        save_config(cfg)

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert reloaded.get("custom_providers") == [{"name": "Local", "base_url": "http://localhost:8080/v1"}]


def test_setup_gateway_skips_service_install_when_systemctl_missing(monkeypatch, capsys):
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:configured-test-token",
        "TELEGRAM_HOME_CHANNEL": "",
        "DISCORD_BOT_TOKEN": "",
        "DISCORD_HOME_CHANNEL": "",
        "SLACK_BOT_TOKEN": "",
        "SLACK_HOME_CHANNEL": "",
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER_ID": "@alice:example.com",
        "MATRIX_PASSWORD": "",
        "MATRIX_ACCESS_TOKEN": "token",
        "BLUEBUBBLES_SERVER_URL": "",
        "BLUEBUBBLES_HOME_CHANNEL": "",
        "WHATSAPP_ENABLED": "",
        "WEBHOOK_ENABLED": "",
    }

    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(setup_mod, "get_env_value", lambda key: env.get(key, ""))
    monkeypatch.setattr(gateway_mod, "get_env_value", lambda key: env.get(key, ""))
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *args, **kwargs: False)
    # Keep the checklist pre-selection (so matrix stays "configured" and the
    # post-config service guidance runs), but stub the migrated plugins'
    # interactive_setup so their wizards don't read real stdin. #41112.
    monkeypatch.setattr(
        setup_mod,
        "prompt_checklist",
        lambda _q, _items, pre_selected=(), **k: list(pre_selected),
    )
    import hermes_cli.gateway as _gw_mod
    monkeypatch.setattr(_gw_mod, "_configure_platform", lambda *a, **k: None)
    monkeypatch.setattr("platform.system", lambda: "Linux")

    monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

    setup_mod.setup_gateway({})

    out = capsys.readouterr().out
    assert "Messaging platforms configured!" in out
    assert "Start the gateway to bring your bots online:" in out
    assert "hermes gateway" in out


def test_setup_gateway_in_container_shows_docker_guidance(monkeypatch, capsys):
    """setup_gateway() in a Docker container shows Docker-specific restart instructions."""
    env = {
        "TELEGRAM_BOT_TOKEN": "123456789:configured-test-token",
        "TELEGRAM_HOME_CHANNEL": "",
        "DISCORD_BOT_TOKEN": "",
        "DISCORD_HOME_CHANNEL": "",
        "SLACK_BOT_TOKEN": "",
        "SLACK_HOME_CHANNEL": "",
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER_ID": "@alice:example.com",
        "MATRIX_PASSWORD": "",
        "MATRIX_ACCESS_TOKEN": "token",
        "BLUEBUBBLES_SERVER_URL": "",
        "BLUEBUBBLES_HOME_CHANNEL": "",
        "WHATSAPP_ENABLED": "",
        "WEBHOOK_ENABLED": "",
    }

    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(setup_mod, "get_env_value", lambda key: env.get(key, ""))
    monkeypatch.setattr(gateway_mod, "get_env_value", lambda key: env.get(key, ""))
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *args, **kwargs: False)
    # Keep the checklist pre-selection (so matrix stays "configured" and the
    # post-config service guidance runs), but stub the migrated plugins'
    # interactive_setup so their wizards don't read real stdin. #41112.
    monkeypatch.setattr(
        setup_mod,
        "prompt_checklist",
        lambda _q, _items, pre_selected=(), **k: list(pre_selected),
    )
    import hermes_cli.gateway as _gw_mod
    monkeypatch.setattr(_gw_mod, "_configure_platform", lambda *a, **k: None)
    monkeypatch.setattr("platform.system", lambda: "Linux")

    monkeypatch.setattr(gateway_mod, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_installed", lambda: False)
    monkeypatch.setattr(gateway_mod, "_is_service_running", lambda: False)

    # Patch is_container at the import location in setup.py
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "is_container", lambda: True)

    setup_mod.setup_gateway({})

    out = capsys.readouterr().out
    assert "Messaging platforms configured!" in out
    assert "docker" in out.lower() or "Docker" in out
    assert "restart" in out.lower()


def test_setup_syncs_custom_provider_removal_from_disk(tmp_path, monkeypatch):
    """Removing the last custom provider in model setup should persist."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()
    config["custom_providers"] = [{"name": "Local", "base_url": "http://localhost:8080/v1"}]
    save_config(config)

    def fake_select():
        cfg = load_config()
        cfg["model"] = {"provider": "openrouter", "default": "anthropic/claude-opus-4.6"}
        cfg["custom_providers"] = []
        save_config(cfg)

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert reloaded.get("custom_providers") == []


def test_setup_cancel_preserves_existing_config(tmp_path, monkeypatch):
    """When the user cancels provider selection, existing config is preserved."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    # Pre-set a provider
    _write_model_config(tmp_path, "openrouter", model_name="gpt-4o")

    config = load_config()
    assert config["model"]["provider"] == "openrouter"

    def fake_select():
        pass  # user cancelled — nothing written to disk

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)

    setup_model_provider(config)
    save_config(config)

    reloaded = load_config()
    assert isinstance(reloaded["model"], dict)
    assert reloaded["model"]["provider"] == "openrouter"
    assert reloaded["model"]["default"] == "gpt-4o"


def test_setup_exception_in_select_gracefully_handled(tmp_path, monkeypatch):
    """If select_provider_and_model raises, setup continues with existing config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    _stub_tts(monkeypatch)

    config = load_config()

    def fake_select():
        raise RuntimeError("something broke")

    monkeypatch.setattr("hermes_cli.main.select_provider_and_model", fake_select)




def test_select_provider_and_model_warns_if_named_custom_provider_disappears(
    tmp_path, monkeypatch, capsys
):
    """If a saved custom provider is deleted mid-selection, show a warning instead of silently doing nothing."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)

    cfg = load_config()
    cfg["custom_providers"] = [{"name": "Local", "base_url": "http://localhost:8080/v1"}]
    save_config(cfg)

    def fake_prompt_provider_choice(choices, default=0):
        current = load_config()
        current["custom_providers"] = []
        save_config(current)
        return next(i for i, label in enumerate(choices) if label.startswith("Local (localhost:8080/v1)"))

    monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda provider: None)
    monkeypatch.setattr("hermes_cli.main._prompt_provider_choice", fake_prompt_provider_choice)
    monkeypatch.setattr(
        "hermes_cli.main._model_flow_named_custom",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("named custom flow should not run")),
    )

    from hermes_cli.main import select_provider_and_model

    select_provider_and_model()

    out = capsys.readouterr().out
    assert "selected saved custom provider is no longer available" in out








def test_tts_setup_configures_pockettts_with_streaming_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = load_config()
    cuda_torch_calls = []

    def fake_prompt_choice(question, choices, default=0, description=None):
        if question == "Select TTS provider:":
            return next(i for i, choice in enumerate(choices) if "PocketTTS" in choice)
        raise AssertionError(f"Unexpected prompt_choice call: {question}")

    def fake_prompt(message, *args, **kwargs):
        raise AssertionError(f"Unexpected prompt call: {message}")

    monkeypatch.setattr(setup_mod, "managed_nous_tools_enabled", lambda: False)
    monkeypatch.setattr(
        setup_mod,
        "get_nous_subscription_features",
        lambda _config: type("Features", (), {"nous_auth_present": False})(),
    )
    monkeypatch.setattr(setup_mod.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(setup_mod, "_install_pockettts_deps", lambda: True)
    monkeypatch.setattr(setup_mod, "_install_parakeet_stt_deps", lambda: True)
    monkeypatch.setattr(setup_mod, "_install_semantic_turn_deps", lambda: True)
    monkeypatch.setattr(
        "hermes_cli.tools_config._ensure_cuda_torch",
        lambda py: cuda_torch_calls.append(py),
    )
    monkeypatch.setattr(setup_mod, "prompt_choice", fake_prompt_choice)
    monkeypatch.setattr(setup_mod, "prompt_yes_no", lambda *args, **kwargs: True)
    monkeypatch.setattr(setup_mod, "prompt", fake_prompt)
    monkeypatch.setattr(setup_mod, "save_config", lambda cfg: None)

    setup_mod.setup_tts(config)

    assert config["tts"]["provider"] == "pockettts"
    assert config["tts"]["pockettts"]["voice"] == "alba"
    assert config["tts"]["pockettts"]["device"] == "cuda"
    assert cuda_torch_calls
    assert config["stt"]["provider"] == "local"
    assert config["stt"]["local"]["device"] == "cuda"
    assert config["stt"]["local"]["compute_type"] == "float16"
    assert config["stt"]["streaming"]["provider"] == "parakeet"
    assert config["stt"]["streaming"]["model"] == "nvidia/parakeet_realtime_eou_120m-v1"
    assert config["stt"]["streaming"]["eou_token"] == "<EOU>"
    assert config["stt"]["streaming"]["port"] == 9090
    assert config["voice"]["semantic_turn"] is True


def test_modal_setup_persists_direct_mode_when_user_chooses_their_own_account(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.setup.managed_nous_tools_enabled", lambda: True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    config = load_config()

    def fake_prompt_choice(question, choices, default=0):
        if question == "Select terminal backend:":
            return 2
        if question == "Select how Modal execution should be billed:":
            return 1
        raise AssertionError(f"Unexpected prompt_choice call: {question}")

    prompt_values = iter(["token-id", "token-secret", ""])

    monkeypatch.setattr("hermes_cli.setup.prompt_choice", fake_prompt_choice)
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_values))
    monkeypatch.setattr("hermes_cli.setup._prompt_container_resources", lambda config: None)
    monkeypatch.setattr(
        "hermes_cli.setup.get_nous_subscription_features",
        lambda config: type("Features", (), {"nous_auth_present": True})(),
    )
    monkeypatch.setitem(
        sys.modules,
        "tools.managed_tool_gateway",
        types.SimpleNamespace(
            is_managed_tool_gateway_ready=lambda vendor: vendor == "modal",
            resolve_managed_tool_gateway=lambda vendor: None,
        ),
    )
    monkeypatch.setitem(sys.modules, "swe_rex", object())

    from hermes_cli.setup import setup_terminal_backend

    setup_terminal_backend(config)

    assert config["terminal"]["backend"] == "modal"
    assert config["terminal"]["modal_mode"] == "direct"


# test_setup_slack_* moved to tests/gateway/test_slack_plugin_setup.py — the
# _setup_slack wizard migrated to the slack plugin's interactive_setup (#41112).


def test_vercel_setup_configures_access_token_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_vercel_env(monkeypatch)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "old-oidc")
    monkeypatch.setitem(sys.modules, "vercel", types.ModuleType("vercel"))
    config = load_config()

    def fake_prompt_choice(question, choices, default=0):
        if question == "Select terminal backend:":
            return 5
        raise AssertionError(f"Unexpected prompt_choice call: {question}")

    prompt_values = iter(["python3.13", "yes", "2", "4096", "token", "project", "team"])

    monkeypatch.setattr("hermes_cli.setup.prompt_choice", fake_prompt_choice)
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_values))

    from hermes_cli.setup import setup_terminal_backend

    setup_terminal_backend(config)

    assert config["terminal"]["backend"] == "vercel_sandbox"
    assert config["terminal"]["vercel_runtime"] == "python3.13"
    assert config["terminal"]["container_disk"] == 51200
    assert os.environ["TERMINAL_VERCEL_RUNTIME"] == "python3.13"
    assert "VERCEL_OIDC_TOKEN" not in os.environ
    assert os.environ["VERCEL_TOKEN"] == "token"
    assert os.environ["VERCEL_PROJECT_ID"] == "project"
    assert os.environ["VERCEL_TEAM_ID"] == "team"


def test_vercel_setup_prefills_project_and_team_from_link_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_vercel_env(monkeypatch)
    project_root = tmp_path / "project"
    nested = project_root / "app" / "src"
    nested.mkdir(parents=True)
    vercel_dir = project_root / ".vercel"
    vercel_dir.mkdir()
    (vercel_dir / "project.json").write_text(
        json.dumps({"projectId": "linked-project", "orgId": "linked-team"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    monkeypatch.setitem(sys.modules, "vercel", types.ModuleType("vercel"))
    config = load_config()
    config["terminal"]["container_disk"] = 999

    def fake_prompt_choice(question, choices, default=0):
        if question == "Select terminal backend:":
            return 5
        raise AssertionError(f"Unexpected prompt_choice call: {question}")

    prompt_values = iter(["node24", "no", "1", "5120", "token", "", ""])
    defaults = {}

    def fake_prompt(message, default="", **kwargs):
        defaults[message] = default
        value = next(prompt_values)
        return value or default

    monkeypatch.setattr("hermes_cli.setup.prompt_choice", fake_prompt_choice)
    monkeypatch.setattr("hermes_cli.setup.prompt", fake_prompt)

    from hermes_cli.setup import setup_terminal_backend

    setup_terminal_backend(config)

    assert config["terminal"]["backend"] == "vercel_sandbox"
    assert config["terminal"]["container_persistent"] is False
    assert config["terminal"]["container_disk"] == 51200
    assert "VERCEL_OIDC_TOKEN" not in os.environ
    assert os.environ["VERCEL_TOKEN"] == "token"
    assert os.environ["VERCEL_PROJECT_ID"] == "linked-project"
    assert os.environ["VERCEL_TEAM_ID"] == "linked-team"
    assert defaults["    Vercel project ID"] == "linked-project"
    assert defaults["    Vercel team ID"] == "linked-team"
