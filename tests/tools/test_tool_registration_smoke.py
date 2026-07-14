"""Regression test for the ``recall_files`` class of bug (fixed in
887bc1b5e): a tool module importing ``from tools import registry`` instead
of ``from tools.registry import registry`` makes ``registry.register()``
raise ``AttributeError`` on the *module* object at import time — silently
swallowed by ``discover_builtin_tools``'s try/except, so the tool never
registers and is unreachable from chat, voice, or the subconscious despite
everything downstream (indexing, CLI, etc.) working fine.

This test walks the same top-level ``tools/*.py`` self-registering module
set that ``tools.registry.discover_builtin_tools`` imports at startup
(``model_tools.py``'s module-load-time call), then asserts every tool name
the registry now knows about has a schema and shows up in
``get_all_tool_names()``. If a future tool module repeats the recall_files
mistake, ``discover_builtin_tools`` logs a warning and moves on — nothing
else fails loudly — so this test exists specifically to fail when that
happens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.registry import _module_registers_tools, discover_builtin_tools, registry


def _expected_self_registering_modules() -> list[str]:
    tools_dir = Path(__file__).resolve().parents[2] / "tools"
    return [
        f"tools.{path.stem}"
        for path in sorted(tools_dir.glob("*.py"))
        if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
        and _module_registers_tools(path)
    ]


class TestToolRegistrationSmoke:
    def test_every_self_registering_module_imports_without_error(self):
        """discover_builtin_tools swallows import errors (logs + continues) —
        that's the exact mechanism that hid the recall_files bug. Re-import
        every candidate module directly (letting exceptions propagate) so a
        broken module fails this test instead of silently vanishing."""
        import importlib

        for module_name in _expected_self_registering_modules():
            importlib.import_module(module_name)

    def test_discover_builtin_tools_actually_imports_every_candidate(self):
        expected = _expected_self_registering_modules()

        imported = discover_builtin_tools()

        missing = set(expected) - set(imported)
        assert not missing, f"modules that should self-register but failed silently: {missing}"

    def test_every_registered_tool_has_a_schema(self):
        discover_builtin_tools()

        names = registry.get_all_tool_names()
        assert names, "expected at least one tool to be registered"

        missing_schema = [name for name in names if registry.get_schema(name) is None]
        assert not missing_schema, f"registered tools with no schema: {missing_schema}"

    def test_every_registered_tool_appears_in_get_all_tool_names(self):
        discover_builtin_tools()

        # Every name in get_all_tool_names() must resolve to a real entry
        # (the two registry views must agree — a tool present in one but not
        # the other means dispatch and introspection would disagree).
        for name in registry.get_all_tool_names():
            assert registry.get_entry(name) is not None

    def test_recall_files_specifically_is_registered_with_a_valid_schema(self):
        """The exact regression this suite guards against: brain_tool.py's
        recall_files silently failing to register because ``from tools import
        registry`` shadowed the module import (fixed in 887bc1b5e — register
        on the registry *instance*, not the module)."""
        discover_builtin_tools()

        assert "recall_files" in registry.get_all_tool_names()
        schema = registry.get_schema("recall_files")
        assert schema is not None
        assert schema["name"] == "recall_files"
        assert "query" in schema["parameters"]["properties"]
        assert registry.get_toolset_for_tool("recall_files") == "memory"

    def test_module_using_module_level_registry_import_would_fail_loudly(self):
        """Reproduces the exact bug shape in isolation, with zero sys.modules/
        sys.path mutation (this suite runs in-process alongside other tests
        that depend on the real ``tools`` package staying intact): a tool
        module that imports the ``tools.registry`` *module* (instead of the
        ``registry`` singleton it exposes) and then calls
        ``registry.register(...)`` hits an AttributeError, because the
        module object has no ``register`` attribute of its own (``register``
        is a method on the ``ToolRegistry`` *class*, not a module-level
        function)."""
        import tools.registry as registry_module

        with pytest.raises(AttributeError):
            registry_module.register(name="broken", toolset="x", schema={}, handler=lambda *a, **k: "{}")
