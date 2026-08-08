"""Command router — handles RPC requests from the bridge.

Routes method calls to the appropriate runtime component:
  get_state, set_mode, set_light, cancel_sleep, set_override,
  get_health, get_diagnostic
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict

from plugins.smart_room.runtime.models import RoomState
from plugins.smart_room.runtime.health import check_device_health
from plugins.smart_room.runtime.state_store import load_location_reports

logger = logging.getLogger(__name__)


def _redact_config(value: Any, key: str = "") -> Any:
    if any(marker in key.lower() for marker in ("key", "secret", "password", "token")):
        return "***"
    if isinstance(value, dict):
        return {name: _redact_config(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


class CommandRouter:
    """Routes RPC method calls to runtime actions."""

    def __init__(self, state: RoomState, config: Dict[str, Any], runtime: "Runtime"):
        self._state = state
        self._config = config
        self._runtime = runtime

    def dispatch(self, method: str, params: Dict[str, Any], request_id: str = "") -> Dict[str, Any]:
        """Route an RPC method to the handler."""
        handler = getattr(self, f"_handle_{method}", None)
        if handler is None:
            result = {"success": False, "error": f"unknown method: {method}"}
            return self._ack(result, request_id)
        try:
            if method == "ping":
                return self._ack(handler(params), request_id)
            return self._ack(handler(params), request_id)
        except Exception as e:
            logger.error("Command %s failed: %s", method, e)
            return self._ack({"success": False, "error": str(e)}, request_id)

    @staticmethod
    def _ack(result: Dict[str, Any], request_id: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "request_id": request_id,
            "status": "success" if result.get("success") else "failed",
            **result,
        }

    def _handle_get_state(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            limit = max(1, min(int(params.get("location_limit", 20)), 500))
        except (TypeError, ValueError):
            return {"success": False, "error": "location_limit must be an integer"}
        state = self._state_dict()
        state["location_history"] = load_location_reports(
            limit=limit,
            since=str(params.get("location_since") or ""),
            until=str(params.get("location_until") or ""),
            zone=str(params.get("location_zone") or ""),
        )
        return {"success": True, "state": state}

    def _state_dict(self) -> Dict[str, Any]:
        lock = getattr(self._runtime, "_state_lock", None)
        if lock is None:
            return self._state.to_dict()
        with lock:
            return self._state.to_dict()

    def _handle_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True}

    def _handle_set_mode(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = params.get("mode", "off")
        if mode not in {"normal", "reading", "focus", "relax", "night", "sleep", "alarm", "off"}:
            return {"success": False, "error": f"invalid mode: {mode}"}
        self._runtime.set_mode(mode, reason="manual")
        return {"success": True, "mode": mode, "state": self._state_dict()}

    def _handle_set_light(self, params: Dict[str, Any]) -> Dict[str, Any]:
        result = self._runtime.set_light(**params, manual=True)
        ack = result if isinstance(result, dict) else {"success": True}
        state = self._state_dict()
        return {**ack, "light": state["light"], "state": state}

    def _handle_cancel_sleep(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._runtime.cancel_sleep()
        return {"success": True, "state": self._state_dict()}

    def _handle_test_welcome(self, params: Dict[str, Any]) -> Dict[str, Any]:
        audience = str(params.get("audience", ""))
        if audience not in {"owner", "guest"}:
            return {"success": False, "error": "audience must be owner or guest"}
        self._runtime.test_welcome(audience)
        return {"success": True, "audience": audience}

    def _handle_set_override(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(params.get("mode") or ("hold_on" if params.get("enabled") else "none"))
        self._runtime.set_override(mode)
        return {"success": True, "override": mode, "state": self._state_dict()}

    def _handle_list_alarms(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "success": True,
            "alarms": self._runtime.list_alarms(),
            "active_alarm": self._runtime.get_active_alarm(),
        }

    def _handle_upsert_alarm(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "alarm": self._runtime.upsert_alarm(params)}

    def _handle_delete_alarm(self, params: Dict[str, Any]) -> Dict[str, Any]:
        alarm_id = str(params.get("id") or "")
        return {"success": self._runtime.delete_alarm(alarm_id), "id": alarm_id}

    def _handle_acknowledge_alarm(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._runtime.acknowledge_alarm(reason=str(params.get("reason") or "awake"))

    def _handle_get_health(self, params: Dict[str, Any]) -> Dict[str, Any]:
        health = check_device_health(self._state, self._config)
        runtime = self._runtime.get_status()
        health["mqtt"]["connected"] = bool(runtime.get("mqtt_connected"))
        health["sound_events"] = runtime.get(
            "sound_events", {"enabled": False, "running": False}
        )
        return {"success": True, "health": health}

    def _handle_get_clap_dataset(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "dataset": self._runtime.get_clap_dataset_status()}

    def _handle_review_clap(self, params: Dict[str, Any]) -> Dict[str, Any]:
        sample_id = str(params.get("id") or "").strip()
        if not sample_id:
            return {"success": False, "error": "clap sample id is required"}
        if not isinstance(params.get("confirmed"), bool):
            return {"success": False, "error": "confirmed must be a boolean"}
        return {
            "success": True,
            "dataset": self._runtime.review_clap_sample(
                sample_id, bool(params["confirmed"])
            ),
        }

    def _handle_phone_location_changed(self, params: Dict[str, Any]) -> Dict[str, Any]:
        required = {"who", "transition", "zone", "at", "delivery_id", "source"}
        missing = sorted(required.difference(params))
        if missing:
            return {"success": False, "error": f"missing fields: {', '.join(missing)}"}
        return self._runtime.phone_location_changed(**{key: params[key] for key in required})

    def _handle_shutdown(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # Let the RPC thread flush this acknowledgement before the daemon's
        # main thread begins teardown.
        timer = threading.Timer(0.1, self._runtime.stop)
        timer.daemon = True
        timer.start()
        return {"success": True}

    def _handle_get_diagnostic(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"success": True, "diagnostic": self._runtime.run_diagnostic()}

    def _handle_vision_observe(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._runtime._vision:
            return {"success": False, "error": "vision service is not configured"}
        return {
            "success": True,
            "vision": self._runtime._vision.observe(
                burst_seconds=float(params.get("burst_seconds", 3)),
                save_evidence=bool(params.get("save_evidence", False)),
                deep=bool(params.get("deep", False)),
                question=str(params.get("question") or ""),
            ),
        }

    def _handle_vision_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._runtime._vision:
            return {"success": False, "error": "vision service is not configured"}
        allowed = {key: params[key] for key in ("limit", "since", "event_type", "identity", "zone") if key in params}
        return {"success": True, "events": self._runtime._vision.history(**allowed)}

    def _handle_vision_faces(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._runtime._vision:
            return {"success": False, "error": "vision service is not configured"}
        action = str(params.get("action") or "list")
        rest = {key: value for key, value in params.items() if key != "action"}
        return {"success": True, "faces": self._runtime._vision.faces(action, **rest)}
