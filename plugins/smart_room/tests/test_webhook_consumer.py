"""Authenticated webhook requests are routed to plugin consumers."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


@pytest.mark.asyncio
async def test_hmac_v2_runs_plugin_hook_after_auth_and_dedup(monkeypatch):
    secret = "smart-room-test-secret"
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1",
        "port": 0,
        "routes": {"smart-room-location": {"secret": secret, "prompt": ""}},
    }))
    calls = []

    def invoke_hook(name, **kwargs):
        calls.append((name, kwargs))
        return [{"handled": True, "status": 200, "body": {"success": True}}]

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    body = json.dumps({
        "who": "shereef", "transition": "arrive", "zone": "home",
        "at": "2026-07-15T12:00:00+00:00",
    }, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
        "X-Request-ID": "delivery-1",
    }
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/webhooks/smart-room-location", data=body, headers=headers)
        assert response.status == 200
        assert await response.json() == {"success": True}
        duplicate = await client.post("/webhooks/smart-room-location", data=body, headers=headers)
        assert (await duplicate.json())["status"] == "duplicate"
    assert len(calls) == 1
    assert calls[0][0] == "on_webhook_received"


@pytest.mark.asyncio
async def test_bad_hmac_never_reaches_plugin_hook(monkeypatch):
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
        "routes": {"smart-room-location": {"secret": "correct", "prompt": ""}},
    }))
    called = False

    def invoke_hook(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke_hook)
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/webhooks/smart-room-location",
            json={"who": "shereef"},
            headers={"X-Webhook-Signature": "bad"},
        )
    assert response.status == 401
    assert called is False
