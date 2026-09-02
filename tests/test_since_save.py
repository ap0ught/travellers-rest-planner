"""SLS-1: the 'since last save' tracker.

TR persists only on sleep — everything between saves exists only in the live
game. The tracker keeps a baseline (last parsed save: item counts + money),
an action log (planner-initiated bridge mutations with verified before/after),
and diffs live bridge reads against the baseline so IN-GAME play is captured
too. A newer save resets both.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from planner.server import app as app_module
from planner.server.app import app
from tests.mock_bridge import MockBridge


@pytest.fixture()
def client():
    return TestClient(app)


def reset_sls():
    app_module._sls_baseline = {}
    app_module._sls_actions = []


def fake_state(mtime=1000.0, counts=None, money=10000):
    return SimpleNamespace(slot_id="File_1", save_mtime=mtime,
                           item_counts=dict(counts or {}), money_copper=money)


# ---------- baseline capture --------------------------------------------------

def test_baseline_captures_new_save_and_clears_actions():
    reset_sls()
    app_module._sls_capture_baseline(fake_state(mtime=1000.0, counts={42: 3}, money=5000))
    app_module._sls_actions.append({"type": "addItem"})
    # same save parsed again -> baseline kept, actions kept
    app_module._sls_capture_baseline(fake_state(mtime=1000.0, counts={42: 3}, money=5000))
    assert app_module._sls_baseline["save_mtime"] == 1000.0
    assert len(app_module._sls_actions) == 1
    # NEWER save -> baseline replaced, actions cleared (now persisted)
    app_module._sls_capture_baseline(fake_state(mtime=2000.0, counts={42: 9}, money=9000))
    assert app_module._sls_baseline["save_mtime"] == 2000.0
    assert app_module._sls_baseline["item_counts"] == {42: 9}
    assert app_module._sls_baseline["money_copper"] == 9000
    assert app_module._sls_actions == []


def test_baseline_ignores_none_state():
    reset_sls()
    app_module._sls_capture_baseline(None)
    assert app_module._sls_baseline == {}


# ---------- action log (from bridge pushes) -----------------------------------

def test_bridge_push_records_mutation_actions_not_queries(client):
    reset_sls()
    app_module._sls_baseline = {"save_mtime": 1000.0, "item_counts": {}, "money_copper": 0}
    client.post("/api/bridge/push", json={"event": {
        "type": "addItem", "data": {"itemId": 42, "count": 5, "before": 0, "after": 5}}})
    client.post("/api/bridge/push", json={"event": {
        "type": "value_read", "data": {"itemId": 42, "count": 5}}})
    assert len(app_module._sls_actions) == 1
    rec = app_module._sls_actions[0]
    assert rec["type"] == "addItem"
    assert rec["itemId"] == 42 and rec["before"] == 0 and rec["after"] == 5


# ---------- /api/since-save endpoint (against the simulator) ------------------

def test_since_save_diffs_live_vs_save(client, monkeypatch):
    reset_sls()
    # baseline: had 10 of item 7, none of 42, 1g in the bank
    app_module._sls_baseline = {"slot": "File_1", "save_mtime": 1000.0,
                                "item_counts": {7: 10}, "money_copper": 10000}
    state = fake_state(mtime=1000.0, counts={7: 10}, money=10000)
    monkeypatch.setattr(app_module, "_load_state_for", lambda s: state)

    # sim: player picked up 5x item 42 and spent all of item 7 in-game; 2.5g banked
    sim = MockBridge(port=0, heartbeat=False, push_events=False,
                     inventory={42: 5}, copper=25000).start()
    try:
        monkeypatch.setenv("TR_BRIDGE_BASE", sim.url)
        # one planner-initiated action since the save
        client.post("/api/bridge/push", json={"event": {
            "type": "shop/buy", "data": {"itemId": 42, "count": 1, "price": 100,
                                         "before_money": 25100, "after_money": 25000,
                                         "before_item": 4, "after_item": 5}}})
        r = client.get("/api/since-save?slot=File_1")
        assert r.status_code == 200
        j = r.json()
        assert j["live"] is True
        assert j["money"]["save_copper"] == 10000
        assert j["money"]["live_copper"] == 25000
        assert j["money"]["delta_copper"] == 15000
        by_id = {c["item_id"]: c for c in j["changed_items"]}
        assert by_id[42]["save_count"] == 0 and by_id[42]["live_count"] == 5
        assert by_id[7]["save_count"] == 10 and by_id[7]["live_count"] == 0
        assert j["changed_count"] == 2
        assert j["action_count"] == 1
        assert j["actions"][0]["type"] == "shop/buy"
    finally:
        sim.stop()


def test_since_save_offline_reports_no_live_diff(client, monkeypatch):
    reset_sls()
    app_module._sls_baseline = {"slot": "File_1", "save_mtime": 1000.0,
                                "item_counts": {7: 10}, "money_copper": 10000}
    state = fake_state(mtime=1000.0, counts={7: 10}, money=10000)
    monkeypatch.setattr(app_module, "_load_state_for", lambda s: state)
    monkeypatch.setenv("TR_BRIDGE_BASE", "http://127.0.0.1:9")  # dead bridge
    r = client.get("/api/since-save?slot=File_1")
    assert r.status_code == 200
    j = r.json()
    assert j["live"] is False
    assert j["changed_items"] == []
    assert j["money"]["delta_copper"] == 0
