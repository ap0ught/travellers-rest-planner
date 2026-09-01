"""G0/G1: bridge heartbeat -> live mode.

The planner derives "live" purely from heartbeat freshness — never by polling
the bridge. Tests flip the mode by posting a beat and by backdating the
heartbeat timestamp, so no game or bridge is needed.
"""
import time

import pytest
from fastapi.testclient import TestClient

from planner.server import app as app_module
from planner.server.app import app, HEARTBEAT_TIMEOUT_S


@pytest.fixture()
def client():
    return TestClient(app)


def reset_heartbeat():
    app_module._bridge_last_beat = 0.0


def test_no_heartbeat_means_save_only(client):
    reset_heartbeat()
    assert not app_module._bridge_live()
    assert app_module._live_reason() == "no_bridge"


def test_heartbeat_sets_live(client):
    reset_heartbeat()
    r = client.post("/api/bridge/heartbeat", json={"type": "heartbeat", "version": "1.2.0"})
    assert r.status_code == 200
    assert r.json()["live"] is True
    assert app_module._bridge_live()
    assert app_module._live_reason() == "live"


def test_heartbeat_timeout_drops_to_save_only(client):
    reset_heartbeat()
    client.post("/api/bridge/heartbeat", json={})
    assert app_module._bridge_live()
    # backdate past the timeout (~3 missed beats) -> save-only, and the
    # reason distinguishes beat_lost (was live) from no_bridge (never seen)
    app_module._bridge_last_beat = time.time() - (HEARTBEAT_TIMEOUT_S + 1.0)
    assert not app_module._bridge_live()
    assert app_module._live_reason() == "beat_lost"


def test_status_carries_heartbeat_fields_even_when_bridge_http_down(client, monkeypatch):
    # Hermetic: dead bridge port so a live bridge/simulator on 8766 can't
    # leak in — the proxy must fail here.
    monkeypatch.setenv("TR_BRIDGE_BASE", "http://127.0.0.1:9")
    reset_heartbeat()
    r = client.get("/api/bridge/status")
    assert r.status_code == 503  # bridge HTTP is down
    j = r.json()
    assert "live" in j
    assert "heartbeat_age_s" in j
    assert j["heartbeat_timeout_s"] == HEARTBEAT_TIMEOUT_S
    assert j["reason"] == "no_bridge"  # never saw a beat (reset above)


def test_share_mode_heartbeat_local_only(client, monkeypatch):
    # Remote callers may not fake liveness in share mode.
    monkeypatch.setenv("TR_SHARE", "1")
    reset_heartbeat()
    r = client.post("/api/bridge/heartbeat", json={})
    assert r.status_code == 403
    assert not app_module._bridge_live()
