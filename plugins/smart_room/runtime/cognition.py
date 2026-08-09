"""Restricted LLM cognition loop for ambiguous Smart Room events.

This is intentionally not a second Marvi agent.  It is a small event worker
with a fixed, room-only toolset, strict iteration/time limits, and a full
decision audit trail.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

from plugins.smart_room.runtime.models import now_iso
from plugins.smart_room.runtime.state_store import publish_cognition
from plugins.smart_room.runtime.vision.history import VisionHistory

logger = logging.getLogger(__name__)


def should_reason(event: Dict[str, Any]) -> bool:
    """Only send uncertain/high-value world events to the cheap model."""
    return str(event.get("type") or "") in {
        "he20_occupied", "presence_detected", "room_entry", "room_presence_unverified",
        "vision_identity_state", "vision_sleep_state", "vision_camera_offline",
        "sensor_vision_conflict", "phone_location_changed",
    }


_TOOLS = [
    {"type": "function", "function": {"name": "observe_room", "description": "Read a fresh camera observation. Use deep=true to understand visitor behavior, movie/computer activity, objects, or an unusual scene after visibility is good.", "parameters": {"type": "object", "properties": {"burst_seconds": {"type": "number", "minimum": 1, "maximum": 5}, "save_evidence": {"type": "boolean"}, "deep": {"type": "boolean"}, "question": {"type": "string", "maxLength": 500}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "set_light", "description": "Control the physical bulb. Use purpose=inspection for temporary 5-12% warm visibility and purpose=final for the intended lasting state. Inspection light is automatically rolled back on failure.", "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}, "brightness": {"type": "integer", "minimum": 0, "maximum": 100}, "color_temp": {"type": "integer", "minimum": 2200, "maximum": 6500}, "purpose": {"type": "string", "enum": ["inspection", "final"]}}, "required": ["purpose"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "set_mode", "description": "Apply a Smart Room scene.", "parameters": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["normal", "reading", "focus", "relax", "night", "sleep", "off"]}}, "required": ["mode"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "speak", "description": "Speak only when the user needs an immediate warning, question, or useful notification.", "parameters": {"type": "object", "properties": {"message": {"type": "string", "maxLength": 300}}, "required": ["message"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "remain_silent", "description": "Finish without disturbing anyone.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "schedule_recheck", "description": "Ask vision to recheck shortly without speaking.", "parameters": {"type": "object", "properties": {"seconds": {"type": "integer", "minimum": 2, "maximum": 120}}, "required": ["seconds"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "vision_history", "description": "Read recent operational visual events.", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "event_type": {"type": "string"}}, "additionalProperties": False}}},
]


class CognitionWorker:
    def __init__(self, config: Dict[str, Any], runtime: Any, vision: Any):
        self.config = config
        self._runtime = runtime
        self._vision = vision
        self._queue: deque[Dict[str, Any]] = deque(maxlen=max(10, int(config.get("queue_size", 50))))
        self._condition = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._history = VisionHistory((runtime._config.get("vision") or {}).get("history") or {})
        self._status: Dict[str, Any] = {"enabled": bool(config.get("enabled", False)), "running": False, "decisions": 0}
        self._active_correlation_id = "smart-room"
        self._inspection_pending = False
        self._inspection_restore: Dict[str, Any] = {}

    def start(self) -> None:
        if not self.config.get("enabled", False) or self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="smart_room_cognition")
        self._thread.start()

    def stop(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def submit(self, event: Dict[str, Any]) -> bool:
        if not self.config.get("enabled", False) or not should_reason(event):
            return False
        with self._condition:
            # Coalesce noisy consecutive sensor events of the same type.
            if self._queue and self._queue[-1].get("type") == event.get("type"):
                self._queue[-1] = event
            else:
                self._queue.append(event)
            self._condition.notify()
        return True

    def status(self) -> Dict[str, Any]:
        result = dict(self._status)
        result["queue_depth"] = len(self._queue)
        return result

    def _run(self) -> None:
        self._status["running"] = True
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._stop, timeout=1)
                if self._stop:
                    break
                event = self._queue.popleft()
                # Let the HE20-triggered camera burst contribute its first
                # visual transition, then reason once from the newest fused
                # event instead of paying for three sensor-edge decisions.
                settle_until = time.monotonic() + max(0.0, float(self.config.get("settle_seconds", 0.6)))
                while not self._stop:
                    remaining = settle_until - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                    if self._queue:
                        event = self._queue.pop()
                        self._queue.clear()
                if self._stop:
                    break
            try:
                self._decide(event)
            except Exception as exc:
                self._status["last_error"] = str(exc)
                logger.exception("Smart Room cognition failed: %s", exc)
            finally:
                self._restore_inspection_if_needed()
        self._status["running"] = False

    def _decide(self, event: Dict[str, Any]) -> None:
        from agent.auxiliary_client import call_llm

        correlation_id = f"think-{uuid.uuid4().hex[:12]}"
        self._active_correlation_id = correlation_id
        with self._runtime._state_lock:
            state = self._runtime._state.to_dict()
        light = state.get("light") or {}
        reflex = event.get("reflex") if isinstance(event.get("reflex"), dict) else {}
        reflex_restore = reflex.get("restore") if isinstance(reflex.get("restore"), dict) else None
        self._inspection_restore = dict(reflex_restore or {
            "on": bool(light.get("on", False)),
            "brightness": int(light.get("brightness", 0)),
            "color_temp": int(light.get("color_temp", 3000)),
            "rgb": light.get("rgb"),
        })
        self._inspection_pending = bool(reflex.get("applied"))
        low_dim = max(3, min(int(self.config.get("inspection_brightness", 8)), 15))
        messages: list[Dict[str, Any]] = [
            {"role": "system", "content": (
                "You are Marvi's private Smart Room reflex brain. Decide from sensor evidence, use tools, and act conservatively. "
                "HE20 mmWave proves movement/presence, not identity. Camera proves only what is visible. Never call a dark camera empty. "
                "An HE20 event may include a reflex inspection light that was already switched on before you were called. Observe immediately; do not switch it on again. "
                f"If darkness blocks an important HE20/entry decision, call set_light with purpose=inspection at {low_dim}% and 2200K, observe, then call set_light with purpose=final to restore/off it when the room is empty, the owner is sleeping, or HE20 was false-positive. "
                "If a person arrived, provide useful light. If the owner watches a movie, prefer dim warm light. Do not disturb sleep. "
                "For an unknown visitor or unclear activity, use a deep observation once visibility is good. Unknown visitors can justify evidence and a short alert. Use remain_silent for ordinary safe changes. Never claim certainty absent evidence. "
                "You control only this room and must finish by calling remain_silent or speak."
            )},
            {"role": "user", "content": json.dumps({"correlation_id": correlation_id, "event": event, "state": state}, ensure_ascii=False)},
        ]
        actions = []
        max_steps = max(1, min(int(self.config.get("max_tool_steps", 6)), 10))
        for _ in range(max_steps):
            response = call_llm(
                task="smart_room_cognition",
                provider=str(self.config.get("provider") or "") or None,
                model=str(self.config.get("model") or "deepseek-v4-flash"),
                messages=messages,
                tools=_TOOLS,
                temperature=float(self.config.get("temperature", 0.1)),
                max_tokens=int(self.config.get("max_tokens", 700)),
                timeout=float(self.config.get("timeout", 25)),
                extra_body={"thinking": {"type": "enabled"}} if self.config.get("thinking", True) else None,
            )
            msg = response.choices[0].message
            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            assistant: Dict[str, Any] = {"role": "assistant", "content": getattr(msg, "content", None)}
            reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
            if not reasoning and isinstance(getattr(msg, "model_extra", None), dict):
                reasoning = msg.model_extra.get("reasoning_content")
            if reasoning:
                assistant["reasoning_content"] = reasoning
            if tool_calls:
                assistant["tool_calls"] = [self._tool_call_dict(call) for call in tool_calls]
            messages.append(assistant)
            if not tool_calls:
                break
            finished = False
            for call in tool_calls:
                args: Dict[str, Any] = {}
                function = getattr(call, "function", None)
                name = str(getattr(function, "name", ""))
                try:
                    args = json.loads(getattr(function, "arguments", "{}") or "{}")
                    result = self._execute(name, args)
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}
                actions.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_call_id": str(getattr(call, "id", "")), "content": json.dumps(result, ensure_ascii=False)})
                finished = finished or name in {"speak", "remain_silent"}
            if finished:
                break
        record = {"id": correlation_id, "at": now_iso(), "type": "smart_room_cognition", "trigger": event, "actions": actions}
        self._history.append(record)
        self._runtime._emit_event("smart_room_cognition", {
            "correlation_id": correlation_id,
            "trigger_type": event.get("type"),
            "actions": actions,
            "summary": "Smart Room cognition decision",
        })
        self._status["decisions"] += 1
        self._status["last_decision_at"] = record["at"]
        self._restore_inspection_if_needed()

    @staticmethod
    def _tool_call_dict(call: Any) -> Dict[str, Any]:
        if isinstance(call, dict):
            function = call.get("function") or {}
            return {"id": str(call.get("id") or ""), "type": "function", "function": {"name": str(function.get("name") or ""), "arguments": str(function.get("arguments") or "{}")}}
        fn = getattr(call, "function", None)
        return {"id": str(getattr(call, "id", "")), "type": "function", "function": {"name": str(getattr(fn, "name", "")), "arguments": str(getattr(fn, "arguments", "{}") or "{}")}}

    def _execute(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "observe_room":
            return {"success": True, "vision": self._vision.observe(
                burst_seconds=float(args.get("burst_seconds", 3)),
                save_evidence=bool(args.get("save_evidence")),
                deep=bool(args.get("deep")),
                question=str(args.get("question") or ""),
            )}
        if name == "set_light":
            allowed = {key: args[key] for key in ("on", "brightness", "color_temp") if key in args}
            if args.get("purpose") == "inspection":
                self._inspection_pending = True
            else:
                self._inspection_pending = False
            return self._runtime.set_light(**allowed, manual=False)
        if name == "set_mode":
            self._runtime.set_mode(str(args["mode"]), reason="cognition")
            return {"success": True, "mode": args["mode"]}
        if name == "speak":
            publish_cognition(str(args["message"]), correlation_id=self._active_correlation_id)
            return {"success": True, "spoken": True}
        if name == "remain_silent":
            return {"success": True, "silent": True, "reason": str(args.get("reason") or "")}
        if name == "schedule_recheck":
            seconds = max(2, min(int(args["seconds"]), 120))
            timer = threading.Timer(seconds, lambda: self.submit({"type": "sensor_vision_conflict", "at": now_iso(), "source": "scheduled_recheck"}))
            timer.daemon = True
            timer.start()
            return {"success": True, "seconds": seconds}
        if name == "vision_history":
            return {"success": True, "events": self._history.query(limit=int(args.get("limit", 10)), event_type=str(args.get("event_type") or ""))}
        raise ValueError(f"unknown cognition tool: {name}")

    def _restore_inspection_if_needed(self) -> None:
        if not self._inspection_pending:
            return
        self._inspection_pending = False
        try:
            self._runtime.set_light(**self._inspection_restore, manual=False)
            logger.info("Rolled back unfinished Smart Room inspection light")
        except Exception:
            logger.exception("Failed to roll back Smart Room inspection light")
