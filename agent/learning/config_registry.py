"""Closed registry and compare-and-set application for learned config."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tools.presence.resource_policy import DEFAULT_HEAVY_APPS

_TIER_RE = re.compile(r"^subconscious\.tiers\.([a-z0-9][a-z0-9_-]{0,79})$")
_MISSING = object()


@dataclass(frozen=True)
class ConfigRule:
    value_type: type
    human: str
    category: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None


_RULES: Dict[str, ConfigRule] = {
    "voice.speaker_id.threshold": ConfigRule(float, "speaker owner threshold", "voice", .30, .80, .01),
    "voice.speaker_id.reject_threshold": ConfigRule(float, "speaker rejection threshold", "voice", .05, .40, .01),
    "presence.heavy_apps": ConfigRule(list, "focus-heavy applications", "presence"),
    "learning.timing.quiet_hours": ConfigRule(list, "proactive quiet hours", "timing"),
}

# Public, inspectable registry shape named by the spec. Internal validation
# uses the frozen ConfigRule values above so callers cannot mutate policy.
REGISTRY: Dict[str, Dict[str, Any]] = {
    path: {
        "type": rule.value_type,
        "min": rule.minimum,
        "max": rule.maximum,
        "step": rule.step,
        "category": rule.category,
        "human": rule.human,
    }
    for path, rule in _RULES.items()
}

_FALLBACKS: Dict[str, Any] = {
    "voice.speaker_id.threshold": .45,
    "voice.speaker_id.reject_threshold": .25,
    "presence.heavy_apps": list(DEFAULT_HEAVY_APPS),
    "learning.timing.quiet_hours": [],
}


def rule_for(path: str) -> Optional[ConfigRule]:
    rule = _RULES.get(path)
    if rule is not None:
        return rule
    if _TIER_RE.fullmatch(path):
        return ConfigRule(str, f"proactivity tier for {path.rsplit('.', 1)[-1]}", "trust")
    return None


def _nested(data: Dict[str, Any], path: str, default: Any = _MISSING) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def current_value(path: str, config: Optional[Dict[str, Any]] = None) -> Any:
    """Return the effective value used for stale-proposal comparison."""
    from hermes_cli.config import load_config

    cfg = config if config is not None else load_config()
    value = _nested(cfg, path)
    if value is not _MISSING:
        return copy.deepcopy(value)
    if _TIER_RE.fullmatch(path):
        return "propose"
    return copy.deepcopy(_FALLBACKS.get(path))


def validate_config_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("config_spec must be an object")
    path = str(spec.get("path") or "")
    rule = rule_for(path)
    if rule is None:
        raise ValueError(f"learned config path is not allowed: {path!r}")
    if spec.get("scope", "user") != "user":
        raise ValueError("learned config scope must be 'user'")
    value = spec.get("value", _MISSING)
    if value is _MISSING:
        raise ValueError("config_spec.value is required")
    if rule.value_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{path} must be a finite number")
        value = float(value)
        if rule.minimum is not None and value < rule.minimum or rule.maximum is not None and value > rule.maximum:
            raise ValueError(f"{path} is outside the allowed range")
        if rule.step:
            base = rule.minimum or 0.0
            units = (value - base) / rule.step
            if abs(units - round(units)) > 1e-7:
                raise ValueError(f"{path} must use increments of {rule.step:g}")
    elif rule.value_type is str:
        if value not in {"notify", "propose", "auto"}:
            raise ValueError("proactivity tier must be notify, propose, or auto")
    elif rule.value_type is list:
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"{path} must be a list of non-empty strings")
        value = list(dict.fromkeys(item.strip() for item in value))
        if len(value) > 100:
            raise ValueError(f"{path} cannot contain more than 100 values")
        if any(len(item) > 200 for item in value):
            raise ValueError(f"{path} values cannot exceed 200 characters")
        if path == "learning.timing.quiet_hours":
            window_re = re.compile(r"^(?:[01]\d|2[0-3]):00-(?:[01]\d|2[0-3]):00$")
            if len(value) > 24 or not all(window_re.fullmatch(item) for item in value):
                raise ValueError("timing quiet hours must be hourly HH:00-HH:00 windows")
    normalized = dict(spec)
    normalized.update({"path": path, "value": value, "scope": "user", "human": rule.human})
    normalized["rationale"] = str(spec.get("rationale") or "").strip()
    if not normalized["rationale"]:
        raise ValueError("config_spec.rationale is required")
    return normalized


def apply_config_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and atomically apply an approved config proposal.

    The stored ``current`` value is a compare-and-set guard: if the user or an
    administrator changed the setting after the proposal was made, acceptance
    fails instead of overwriting the newer decision.
    """
    normalized = validate_config_spec(spec)
    path = normalized["path"]
    from hermes_cli import managed_scope
    from hermes_cli.config import _set_nested, is_managed, load_config, read_raw_config, save_config

    if is_managed():
        raise ValueError("configuration is managed and cannot be changed")
    if managed_scope.is_key_managed(path):
        raise ValueError(f"{path} is managed by an administrator")
    effective = load_config()
    actual = current_value(path, effective)
    if "current" not in normalized or normalized["current"] != actual:
        raise ValueError(f"stale config proposal for {path}: current value is {actual!r}")

    proposed = normalized["value"]
    owner = proposed if path == "voice.speaker_id.threshold" else current_value("voice.speaker_id.threshold", effective)
    reject = proposed if path == "voice.speaker_id.reject_threshold" else current_value("voice.speaker_id.reject_threshold", effective)
    try:
        owner, reject = float(owner), float(reject)
    except (TypeError, ValueError) as exc:
        raise ValueError("existing speaker threshold configuration is invalid") from exc
    if reject >= owner:
        raise ValueError("speaker reject threshold must remain below owner threshold")

    raw = read_raw_config()
    _set_nested(raw, path, proposed)
    save_config(raw, preserve_keys={tuple(path.split("."))})
    return {"path": path, "value": proposed, "previous": actual, "human": normalized["human"]}
