"""Tests for the [REQ-SLOW] slow-request logging middleware in web_server.py.

The middleware wraps every HTTP request (WebSocket endpoints are exempt --
ASGI only routes "http"-scope traffic through HTTP middleware) and logs a
single grep-friendly line for anything taking longer than
``_SLOW_REQUEST_THRESHOLD_MS`` (500ms), throttled per (method, route).
"""

from __future__ import annotations

import logging
import time

import pytest


class TestSlowRequestMiddleware:
    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli import web_server as ws

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

        self.ws = ws
        self.app = ws.app
        self.client = TestClient(ws.app)
        self.client.headers[ws._SESSION_HEADER_NAME] = ws._SESSION_TOKEN

        # Fresh throttle state per test so earlier tests' timestamps don't
        # suppress this test's expected [REQ-SLOW] line.
        ws._slow_request_last_logged.clear()

        yield

        # Tear down any synthetic test route(s) so they don't leak into
        # other tests sharing the module-level `app`.
        self.app.router.routes = [
            r for r in self.app.router.routes
            if "__test_" not in getattr(r, "path", "")
        ]
        ws._slow_request_last_logged.clear()

    def _add_route(self, path: str, *, delay: float = 0.0):
        """Register a synthetic test-only route and make sure it's actually
        reachable.

        The SPA catch-all ``/{full_path:path}`` (mounted by ``mount_spa()``
        at import time -- see web_server.py's ``serve_spa``) intercepts any
        unmatched ``/api/*`` path with its own 404 JSON *before* a route
        appended later would ever be tried, since Starlette matches routes
        in registration order. Move the newly-added route to the front of
        ``app.router.routes`` so it wins the match.
        """

        @self.app.get(path)
        async def _handler():  # pragma: no cover - trivial
            if delay:
                time.sleep(delay)
            return {"ok": True}

        routes = self.app.router.routes
        new_route = routes[-1]
        routes.remove(new_route)
        routes.insert(0, new_route)

    def test_slow_request_logs_req_slow_line(self, caplog):
        self._add_route("/api/__test_slow_route", delay=0.6)
        caplog.set_level(logging.WARNING, logger="hermes_cli.web_server")

        resp = self.client.get("/api/__test_slow_route")
        assert resp.status_code == 200

        lines = [r.getMessage() for r in caplog.records if "[REQ-SLOW]" in r.getMessage()]
        assert lines, "expected a [REQ-SLOW] log line for a >500ms request"
        msg = lines[-1]
        assert "method=GET" in msg
        assert "route=/api/__test_slow_route" in msg
        assert "duration_ms=" in msg
        assert "loop_lag=" in msg

    def test_fast_request_does_not_log(self, caplog):
        self._add_route("/api/__test_fast_route")
        caplog.set_level(logging.WARNING, logger="hermes_cli.web_server")
        resp = self.client.get("/api/__test_fast_route")
        assert resp.status_code == 200
        lines = [r.getMessage() for r in caplog.records if "[REQ-SLOW]" in r.getMessage()]
        assert not lines

    def test_repeated_slow_requests_are_throttled(self, caplog):
        self._add_route("/api/__test_slow_route_throttle", delay=0.55)
        caplog.set_level(logging.WARNING, logger="hermes_cli.web_server")

        for _ in range(3):
            resp = self.client.get("/api/__test_slow_route_throttle")
            assert resp.status_code == 200

        lines = [r.getMessage() for r in caplog.records if "[REQ-SLOW]" in r.getMessage()]
        # Three requests within the 5s throttle window for the same route
        # should produce exactly one log line, not three.
        assert len(lines) == 1

    def test_loop_lag_flag_reflects_watchdog_state(self, caplog, monkeypatch):
        self._add_route("/api/__test_slow_route_lag", delay=0.6)
        caplog.set_level(logging.WARNING, logger="hermes_cli.web_server")

        from gateway import loop_watchdog

        # Force the watchdog to report a lag event covering the whole test.
        monkeypatch.setattr(loop_watchdog, "lag_fired_since", lambda start: True)

        resp = self.client.get("/api/__test_slow_route_lag")
        assert resp.status_code == 200

        lines = [r.getMessage() for r in caplog.records if "[REQ-SLOW]" in r.getMessage()]
        assert lines
        assert "loop_lag=True" in lines[-1]
