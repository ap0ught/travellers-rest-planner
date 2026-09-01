"""SEC-1: share-mode token gate on game-mutating endpoints.

Default (localhost) mode must not require a token — writes proceed to the
bridge and fail with the offline-bridge 503 refusal, never 401. In share mode
(TR_SHARE=1) remote clients must present the per-run SHARE_TOKEN
(X-Share-Token header or ?token= query) or writes are refused with 401, and
/api/bridge/push is local-only (403 for remote callers).
"""
import pytest
from fastapi.testclient import TestClient

from planner.server import app as app_module
from planner.server.app import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def share_mode(monkeypatch):
    monkeypatch.setenv("TR_SHARE", "1")


def test_default_mode_no_token_needed(client):
    # TestClient's client host is "testclient" (non-local), yet default mode
    # must not gate: the server is bound to 127.0.0.1 anyway.
    r = client.post("/api/cheat/money", json={"copper": 100})
    assert r.status_code != 401
    assert r.status_code == 503  # proceeds to the offline-bridge refusal


def test_share_mode_write_requires_token(client, share_mode):
    r = client.post("/api/shop/buy", json={"itemId": 1, "count": 1, "price": 10})
    assert r.status_code == 401


def test_share_mode_wrong_token_rejected(client, share_mode):
    r = client.post(
        "/api/shop/sell", json={"itemId": 1, "count": 1, "price": 10},
        headers={"X-Share-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_share_mode_correct_token_passes_gate(client, share_mode):
    r = client.post(
        "/api/cheat/money", json={"copper": 100},
        headers={"X-Share-Token": app_module.SHARE_TOKEN},
    )
    assert r.status_code != 401
    assert r.status_code == 503  # gate passed; offline-bridge refusal after


def test_share_mode_token_via_query_param(client, share_mode):
    r = client.post(
        f"/api/cheat/seed?token={app_module.SHARE_TOKEN}",
        json={"itemId": 5, "count": 3},
    )
    assert r.status_code != 401
    assert r.status_code == 503


def test_share_mode_bridge_push_local_only(client, share_mode):
    # Remote/tunneled callers may not spoof bridge events in share mode.
    r = client.post("/api/bridge/push", json={"event": {"type": "addItem", "data": {}}})
    assert r.status_code == 403


def test_default_mode_bridge_push_open(client):
    # Default mode is bound to 127.0.0.1 — pushes are fine without checks.
    r = client.post("/api/bridge/push", json={"event": {"type": "addItem", "data": {}}})
    assert r.status_code == 200
