"""Authenticated desktop API for Smart Room settings and controls."""

from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_cli.config import save_env_value
from plugins.smart_room.bridge import call_runtime
from plugins.smart_room.process_manager import start_supervisor, status, stop_supervisor
from plugins.smart_room.runtime.state_store import load_config

router = APIRouter()


class ModeBody(BaseModel):
    mode: str


class OverrideBody(BaseModel):
    mode: Literal["none", "hold_on", "hold_off"]


class AlarmBody(BaseModel):
    id: Optional[str] = None
    name: str = Field(min_length=1, max_length=80)
    time: str
    recurrence: Literal["once", "daily"]
    date: Optional[str] = None
    enabled: bool = True
    duration_minutes: int = Field(default=30, ge=1, le=180)


class WelcomeTestBody(BaseModel):
    audience: Literal["owner", "guest"]


class ClapReviewBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    confirmed: bool


class VisionObserveBody(BaseModel):
    deep: bool = False
    question: str = Field(default="", max_length=500)
    save_evidence: bool = False


class FaceEnrollBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner: bool = False
    samples: int = Field(default=8, ge=3, le=30)


class FaceReviewBody(BaseModel):
    event_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=80)
    owner: bool = False
    reject: bool = False


class FaceReviewAllBody(BaseModel):
    name: str = Field(default="", max_length=80)
    owner: bool = False
    reject: bool = False


class FaceSamplingBody(BaseModel):
    enabled: bool


class LightBody(BaseModel):
    on: Optional[bool] = None
    brightness: Optional[int] = Field(default=None, ge=0, le=100)
    color_temp: Optional[int] = Field(default=None, ge=2200, le=6500)
    rgb: Optional[List[int]] = None


class SecretsBody(BaseModel):
    bulb_key: Optional[str] = Field(default=None, max_length=256)
    he20_key: Optional[str] = Field(default=None, max_length=256)
    mqtt_username: Optional[str] = Field(default=None, max_length=256)
    mqtt_password: Optional[str] = Field(default=None, max_length=4096)


async def _rpc(method: str, params: dict) -> dict:
    try:
        return await asyncio.to_thread(call_runtime, method, params)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/status")
async def get_status() -> dict:
    process = await asyncio.to_thread(status)
    if not process.get("alive"):
        return {"runtime": process, "state": None, "health": None}
    state, health = await asyncio.gather(
        asyncio.to_thread(call_runtime, "get_state", {}),
        asyncio.to_thread(call_runtime, "get_health", {}),
        return_exceptions=True,
    )
    ready = not isinstance(state, Exception) and not isinstance(health, Exception)
    process["ready"] = ready
    return {
        "runtime": process,
        "state": state.get("state") if isinstance(state, dict) else None,
        "health": health.get("health") if isinstance(health, dict) else None,
    }


@router.post("/mode")
async def set_mode(body: ModeBody) -> dict:
    if body.mode not in {"normal", "reading", "focus", "relax", "night", "sleep", "alarm", "off"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    return await _rpc("set_mode", {"mode": body.mode})


@router.post("/light")
async def set_light(body: LightBody) -> dict:
    params = body.model_dump(exclude_none=True)
    if not params:
        raise HTTPException(status_code=400, detail="at least one light field is required")
    if body.rgb is not None and (len(body.rgb) != 3 or any(not 0 <= value <= 255 for value in body.rgb)):
        raise HTTPException(status_code=400, detail="rgb must contain three values from 0 to 255")
    return await _rpc("set_light", params)


@router.post("/override")
async def set_override(body: OverrideBody) -> dict:
    return await _rpc("set_override", {"mode": body.mode})


@router.post("/cancel-sleep")
async def cancel_sleep() -> dict:
    return await _rpc("cancel_sleep", {})


@router.post("/welcome/test")
async def test_welcome(body: WelcomeTestBody) -> dict:
    return await _rpc("test_welcome", {"audience": body.audience})


@router.get("/clap-dataset")
async def get_clap_dataset() -> dict:
    return await _rpc("get_clap_dataset", {})


@router.post("/clap-dataset/review")
async def review_clap(body: ClapReviewBody) -> dict:
    return await _rpc("review_clap", body.model_dump())


@router.get("/vision/preview")
async def vision_preview() -> dict:
    return await _rpc("vision_preview", {"width": 720, "quality": 72})


@router.post("/vision/observe")
async def vision_observe(body: VisionObserveBody) -> dict:
    return await _rpc("vision_observe", {**body.model_dump(), "burst_seconds": 3})


@router.get("/vision/faces")
async def vision_faces() -> dict:
    return await _rpc("vision_faces", {"action": "list"})


@router.post("/vision/faces/enroll")
async def vision_face_enroll(body: FaceEnrollBody) -> dict:
    return await _rpc("vision_faces", {"action": "enroll_current", **body.model_dump()})


@router.post("/vision/faces/review")
async def vision_face_review(body: FaceReviewBody) -> dict:
    return await _rpc("vision_faces", {"action": "review", **body.model_dump()})


@router.post("/vision/faces/review-all")
async def vision_face_review_all(body: FaceReviewAllBody) -> dict:
    return await _rpc("vision_faces", {"action": "review_all", **body.model_dump()})


@router.put("/vision/faces/sampling")
async def vision_face_sampling(body: FaceSamplingBody) -> dict:
    return await _rpc("vision_faces", {"action": "set_sampling", **body.model_dump()})


@router.get("/vision/faces/pending/{event_id}/preview")
async def vision_face_pending_preview(event_id: str) -> dict:
    return await _rpc("vision_faces", {"action": "pending_preview", "event_id": event_id})


@router.delete("/vision/faces/{name}")
async def vision_face_delete(name: str) -> dict:
    return await _rpc("vision_faces", {"action": "delete", "name": name})


@router.get("/alarms")
async def list_alarms() -> dict:
    return await _rpc("list_alarms", {})


@router.put("/alarms")
async def upsert_alarm(body: AlarmBody) -> dict:
    return await _rpc("upsert_alarm", body.model_dump(exclude_none=True))


@router.delete("/alarms/{alarm_id}")
async def delete_alarm(alarm_id: str) -> dict:
    return await _rpc("delete_alarm", {"id": alarm_id})


@router.post("/alarms/acknowledge")
async def acknowledge_alarm() -> dict:
    return await _rpc("acknowledge_alarm", {"reason": "desktop"})


@router.post("/apply")
async def apply_config() -> dict:
    config = load_config()
    if not config.get("enabled", False):
        await asyncio.to_thread(stop_supervisor)
        return {"ok": True, "enabled": False}
    await asyncio.to_thread(stop_supervisor)
    result = await asyncio.to_thread(start_supervisor, config)
    return {"ok": True, "enabled": True, "runtime": result}


@router.put("/secrets")
async def save_secrets(body: SecretsBody) -> dict:
    mapping = {
        "bulb_key": "SMART_ROOM_TUYA_BULB_KEY",
        "he20_key": "SMART_ROOM_TUYA_HE20_KEY",
        "mqtt_username": "SMART_ROOM_MQTT_USERNAME",
        "mqtt_password": "SMART_ROOM_MQTT_PASSWORD",
    }
    values = body.model_dump(exclude_none=True)
    for field, env_name in mapping.items():
        if field in values:
            await asyncio.to_thread(save_env_value, env_name, values[field])
    return {"ok": True, "saved": sorted(values)}
