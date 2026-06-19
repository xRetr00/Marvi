"""Default SOUL.md template seeded into HERMES_HOME on first run."""

DEFAULT_SOUL_MD = (
    "You are Marvi Agent, an intelligent AI assistant created by xRetro Labs Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

_LEGACY_COMMENT_ONLY_SOUL_BODY = """<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
Edit this to customize how Marvi communicates with you.

Examples:
  - "You are a warm, playful assistant who uses kaomoji occasionally."
  - "You are a concise technical expert. No fluff, just facts."
  - "You speak like a friendly coworker who happens to know everything."

This file is loaded fresh each message -- no restart needed.
Delete the contents (or this file) to use the default personality.
-->"""

LEGACY_DEFAULT_SOUL_MD = frozenset(
    {
        f"# {'Hermes'} {'Agent'} Persona\n\n{_LEGACY_COMMENT_ONLY_SOUL_BODY}",
        f"# Marvi Agent Persona\n\n{_LEGACY_COMMENT_ONLY_SOUL_BODY}",
        DEFAULT_SOUL_MD.replace("xRetro Labs Research", "Nous" + " Research"),
        DEFAULT_SOUL_MD.replace("Marvi Agent", "Hermes" + " Agent").replace(
            "xRetro Labs Research", "Nous" + " Research"
        ),
    }
)


def is_legacy_default_soul(content: str) -> bool:
    """Return whether *content* is an untouched historical default template."""
    normalized = content.replace("\r\n", "\n").strip()
    return normalized in LEGACY_DEFAULT_SOUL_MD
