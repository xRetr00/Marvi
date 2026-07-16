"""Compatibility name for the closed learned-config registry."""

from .config_registry import ConfigRule, REGISTRY, apply_config_spec, current_value, rule_for, validate_config_spec

__all__ = ["ConfigRule", "REGISTRY", "apply_config_spec", "current_value", "rule_for", "validate_config_spec"]
