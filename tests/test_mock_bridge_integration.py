"""Integration tests against the bridge simulator — no game needed.

The bridge is just an HTTP contract, so tests/mock_bridge.py simulates
Plugin.cs 1.2.0 (verified before/after, targeted reads, heartbeat) against
an in-memory game state.

Level 1 — planner -> bridge (TestClient + TR_BRIDGE_BASE at an ephemeral
sim): writes flow through the bridge and return verified before/after;
an offline bridge refuses with 503.

Level 2 — full stack (real uvicorn planner + sim heartbeat): live mode
flips ON from heartbeats and OFF when they stop — even while the sim's HTTP
still answers, proving mode authority is the heartbeat, not reachability.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn
from fastapi.testclient import TestClient

from planner.server import app as app_module
from planner.server.app import app
from tests.mock_bridge import MockBridge


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def sim():
    mb = MockBridge(port=0, heartbeat=False, push_events=False).start()
    yield mb
    mb.stop()


def wait_until(fn, timeout=6.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# ---------- Level 1: planner -> simulated bridge ------------------------------

def test_cheat_seed_flows_through_sim_with_verified_before_after(client, sim, monkeypatch):
    monkeypatch.setenv("TR_BRIDGE_BASE", sim.url)
    r = client.post("/api/cheat/seed", json={"itemId": 42, "count": 5})
    assert r.status_code == 200
    j = r.json()
    assert j["bridge"] is True and j["realtime"] is True
    assert j["before"] == 0 and j["after"] == 5
    # second grant compounds — verified read-back each time
    r2 = client.post("/api/cheat/seed", json={"itemId": 42, "count": 5})
    assert r2.json()["before"] == 5 and r2.json()["after"] == 10


def test_cheat_money_set_and_add_through_sim(client, sim, monkeypatch):
    monkeypatch.setenv("TR_BRIDGE_BASE", sim.url)
    r = client.post("/api/cheat/money", json={"copper": 25000, "action": "set"})
    j = r.json()
    assert r.status_code == 200 and j["bridge"] is True
    assert j["before"] == 10000 and j["after"] == 25000  # sim starts at 1g
    r2 = client.post("/api/cheat/money", json={"copper": 5000, "action": "add"})
    j2 = r2.json()
    assert j2["before"] == 25000 and j2["after"] == 30000


def test_shop_buy_insufficient_funds_surfaces_bridge_error(client, sim, monkeypatch):
    monkeypatch.setenv("TR_BRIDGE_BASE", sim.url)
    r = client.post("/api/shop/buy", json={"itemId": 1, "count": 1, "price": 99999})
    assert r.status_code == 500
    assert "need 99999" in r.json()["error"]


def test_shop_sell_before_after_money_and_item(client, monkeypatch):
    mb = MockBridge(port=0, heartbeat=False, push_events=False,
                    inventory={7: 10}, copper=10000).start()
    try:
        monkeypatch.setenv("TR_BRIDGE_BASE", mb.url)
        r = client.post("/api/shop/sell", json={"itemId": 7, "count": 3, "price": 150})
        j = r.json()
        assert r.status_code == 200 and j["bridge"] is True
        assert j["before"]["money"] == 10000 and j["after"]["money"] == 10150
        assert j["before"]["item"] == 10 and j["after"]["item"] == 7
    finally:
        mb.stop()


def test_offline_bridge_refuses_503_never_patches_save(client, monkeypatch):
    monkeypatch.setenv("TR_BRIDGE_BASE", "http://127.0.0.1:9")  # nothing listens
    r = client.post("/api/cheat/seed", json={"itemId": 42, "count": 5})
    assert r.status_code == 503
    assert "game not running" in r.json()["error"]
    assert r.json()["bridge"] is False


def test_bridge_status_proxies_sim(client, sim, monkeypatch):
    monkeypatch.setenv("TR_BRIDGE_BASE", sim.url)
    r = client.get("/api/bridge/status")
    assert r.status_code == 200
    j = r.json()
    assert j["bridge"] is True
    assert j["version"] == "1.2.0-sim"
    assert "live" in j and "heartbeat_age_s" in j


def test_targeted_value_read_via_sim(client, monkeypatch):
    mb = MockBridge(port=0, heartbeat=False, push_events=False,
                    inventory={3031: 12}, copper=10000).start()
    try:
        monkeypatch.setenv("TR_BRIDGE_BASE", mb.url)
        r = client.post("/api/cheat/seed", json={"itemId": 3031, "count": 3})
        assert r.json()["after"] == 15
    finally:
        mb.stop()


# ---------- Level 2: full stack — heartbeat flips live mode -------------------

def test_full_stack_heartbeat_flips_live_mode(monkeypatch):
    monkeypatch.setenv("TR_HEARTBEAT_TIMEOUT", "1.2")
    app_module._bridge_last_beat = 0.0

    config = uvicorn.Config("planner.server.app:app", host="127.0.0.1",
                            port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: server.started), "planner server did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        planner_url = f"http://127.0.0.1:{port}"

        def status() -> dict:
            try:
                with urllib.request.urlopen(planner_url + "/api/bridge/status", timeout=2) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())

        # No bridge at all -> save-only (the normal state, not an error)
        assert status()["live"] is False

        sim = MockBridge(port=0, heartbeat=True, heartbeat_interval=0.3,
                         planner_url=planner_url, push_events=False).start()
        try:
            # Heartbeats arriving -> live within a couple of beats
            assert wait_until(lambda: status()["live"] is True), "heartbeat never flipped live"

            # Heartbeat stops, but the sim's HTTP keeps answering: mode must
            # still drop to save-only — heartbeat is the authority, not
            # reachability (the G0 invariant).
            sim.stop_heartbeat()
            assert wait_until(lambda: status()["live"] is False), "stale heartbeat never dropped to save-only"
            with urllib.request.urlopen(sim.url + "/ping", timeout=2) as r:
                assert json.loads(r.read())["ok"] is True
        finally:
            sim.stop()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        app_module._bridge_last_beat = 0.0
