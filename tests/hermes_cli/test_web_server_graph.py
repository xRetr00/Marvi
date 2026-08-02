"""Tests for GET /api/memory/graph (graph-mind spec §2.5).

Follows the ``TestClient`` + ``_isolate_hermes_home`` pattern used by
``tests/hermes_cli/test_web_server_episodic.py``. Graph nodes/edges are
seeded via the real (per-test, tempdir-isolated) graph store rather than
mocking it, so these tests also exercise the store <-> endpoint wiring.
"""

from unittest.mock import patch

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")

    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


class TestMemoryGraphEndpoint:
    def test_empty_store_returns_note(self, client):
        resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["nodes"] == []
        assert data["edges"] == []
        assert "note" in data and data["note"]

    def test_no_focus_returns_top_salience_subgraph(self, client):
        from agent.memory.graph import add_edge, upsert_node

        a = upsert_node(type="project", label="NeuDocs", salience=0.9)
        b = upsert_node(type="org", label="bakery-job", salience=0.5)
        add_edge(a, b, "funds")

        resp = client.get("/api/memory/graph")

        assert resp.status_code == 200
        data = resp.json()
        labels = {n["label"] for n in data["nodes"]}
        assert labels == {"NeuDocs", "bakery-job"}
        assert len(data["edges"]) == 1
        assert data["edges"][0] == {"src": a, "dst": b, "relation": "funds", "weight": 1.0}
        for node in data["nodes"]:
            assert set(node) == {"id", "type", "label", "summary", "salience", "source_kind", "source_ref"}

    def test_focus_returns_subgraph_around_that_node(self, client):
        from agent.memory.graph import add_edge, upsert_node

        center = upsert_node(type="project", label="NeuDocs")
        neighbor = upsert_node(type="org", label="bakery-job")
        add_edge(center, neighbor, "funds")
        unrelated = upsert_node(type="fact", label="unrelated fact")

        resp = client.get("/api/memory/graph", params={"focus": "NeuDocs", "depth": 1})

        data = resp.json()
        labels = {n["label"] for n in data["nodes"]}
        assert labels == {"NeuDocs", "bakery-job"}
        assert "unrelated fact" not in labels

    def test_focus_with_no_matching_node_returns_empty_with_note(self, client):
        resp = client.get("/api/memory/graph", params={"focus": "does-not-exist"})

        data = resp.json()
        assert data["nodes"] == []
        assert data["edges"] == []
        assert "note" in data

    def test_type_filter_on_default_subgraph(self, client):
        from agent.memory.graph import upsert_node

        upsert_node(type="project", label="NeuDocs")
        upsert_node(type="person", label="Shereef")

        resp = client.get("/api/memory/graph", params={"type": "project"})

        data = resp.json()
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["type"] == "project"

    def test_invalid_type_returns_400(self, client):
        resp = client.get("/api/memory/graph", params={"type": "not-a-type"})
        assert resp.status_code == 400

    def test_failure_returns_structured_500(self, client):
        with patch(
            "hermes_cli.web_server._read_memory_graph_sync",
            side_effect=RuntimeError("boom"),
        ):
            resp = client.get("/api/memory/graph")

        assert resp.status_code == 500
        assert "boom" not in resp.json()["detail"]

    def test_edit_and_delete_node(self, client):
        from agent.memory.graph import archived, get_node, upsert_node

        node_id = upsert_node(type="fact", label="Old label", summary="Old summary")
        edited = client.put(
            "/api/memory/graph/node",
            json={"id": node_id, "type": "project", "label": "New label", "summary": "New summary", "salience": 0.8},
        )

        assert edited.status_code == 200
        assert edited.json()["node"]["label"] == "New label"
        assert get_node(node_id)["type"] == "project"

        deleted = client.request("DELETE", "/api/memory/graph/node", json={"id": node_id})
        assert deleted.status_code == 200
        assert get_node(node_id) is None
        assert archived()[0]["orig_node_id"] == node_id
