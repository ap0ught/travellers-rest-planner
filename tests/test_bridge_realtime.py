"""Bridge debugging + realtime single-file API tests.

Covers new endpoints added for PlannerBridge graphical feedback:
  POST /api/bridge/push  — rebroadcast as bridge_event
  GET  /api/bridge/status /api/bridge/events
  GET  /api/debug/saves
  GET  /api/saves (now single-file)
plus WS payload shape and error handling.
"""
import os
import time
import json
import tempfile
import pathlib
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from planner.server.app import app
from planner.catalog import load_catalog


@pytest.fixture(scope="module")
def client():
    load_catalog.cache_clear()
    return TestClient(app)


def test_bridge_push_rebroadcasts_bridge_event(client):
    # POST a synthetic bridge event, ensure 200 and that next ws would get bridge_event
    # Since TestClient can't easily test WS push, we test the HTTP contract
    payload = {"type": "bridge_event", "event": {"type": "addItem", "data": {"itemId": 412, "count": 10}}}
    r = client.post("/api/bridge/push", json=payload)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ok") is True
    assert j.get("broadcast") is True

    # variant shape: planner bridge sends {"event": {...}, "type": ...}
    payload2 = {"event": {"type": "addMoney", "data": {"copper": 50000}}, "type": "bridge_event"}
    r2 = client.post("/api/bridge/push", json=payload2)
    assert r2.status_code == 200


def test_bridge_push_minimal_payload(client):
    # Minimal payload without explicit type
    r = client.post("/api/bridge/push", json={"event": {"type": "shop/buy", "data": {"itemId": 100}}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_bridge_status_when_bridge_down(client):
    # In CI no BepInEx bridge is running, should return 503 JSON
    # We don't mock, we expect either 200 if dev machine has bridge, or 503
    r = client.get("/api/bridge/status")
    assert r.status_code in (200, 503)
    j = r.json()
    if r.status_code == 503:
        assert j.get("bridge") is False
        assert "error" in j
    else:
        assert j.get("bridge") is True


def test_bridge_status_proxy_success_with_mock(client):
    fake = {"ok": True, "version": "1.1.0", "uptime_s": 123.4, "requests": 42, "single_file": True}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda *a: False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        r = client.get("/api/bridge/status")
        assert r.status_code == 200
        j = r.json()
        assert j["bridge"] is True
        assert j["version"] == "1.1.0"
        assert j["requests"] == 42


def test_bridge_events_when_bridge_down(client):
    r = client.get("/api/bridge/events")
    assert r.status_code in (200, 503)
    if r.status_code == 503:
        assert "error" in r.json()


def test_bridge_events_proxy_success(client):
    fake_events = {"ok": True, "events": [{"ts": "2026-08-31T00:00:00Z", "type": "addItem", "data": {"itemId": 1}}]}
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_events).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda *a: False
    with patch("urllib.request.urlopen", return_value=mock_resp):
        r = client.get("/api/bridge/events")
        assert r.status_code == 200
        assert len(r.json()["events"]) == 1


def test_debug_saves_returns_single_file_info(client, monkeypatch, tmp_path):
    # Point planner to a temp single-file save root
    root = str(tmp_path)
    slot_dir = os.path.join(root, "File_1")
    os.makedirs(slot_dir)
    # create fake save
    p = os.path.join(slot_dir, "SaveFile-1-1-2026-0-0-1.save")
    pathlib.Path(p).write_bytes(b"fake")
    monkeypatch.setenv("TR_SAVES_DIR", root)
    r = client.get("/api/debug/saves")
    assert r.status_code == 200
    j = r.json()
    assert "single_file_mode" in j
    assert j["single_file_mode"] is True
    assert j["slot"] is not None
    assert j["slot"]["slot_id"] == "File_1"
    assert j["slot"]["latest_file"].endswith(".save")
    assert "all_slots_scanned" in j
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)


def test_debug_saves_no_save_returns_null_slot(client, monkeypatch, tmp_path):
    empty = str(tmp_path / "empty2")
    os.makedirs(empty)
    monkeypatch.setenv("TR_SAVES_DIR", empty)
    r = client.get("/api/debug/saves")
    assert r.status_code == 200
    j = r.json()
    assert j["slot"] is None
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)


def test_saves_endpoint_only_file_1_in_single_mode(client, monkeypatch, tmp_path):
    root = str(tmp_path)
    for name in ["File_1", "File_2", "SaveAnywhere_Manual_1"]:
        d = os.path.join(root, name)
        os.makedirs(d)
        pathlib.Path(os.path.join(d, "SaveFile-1.save")).write_bytes(b"x")
    monkeypatch.setenv("TR_SAVES_DIR", root)
    r = client.get("/api/saves")
    assert r.status_code == 200
    j = r.json()
    assert len(j) == 1
    assert j[0]["slot_id"] == "File_1"
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)


def test_saves_endpoint_legacy_env_shows_all(monkeypatch, tmp_path):
    root = str(tmp_path)
    for name in ["File_1", "File_2"]:
        d = os.path.join(root, name)
        os.makedirs(d)
        pathlib.Path(os.path.join(d, "SaveFile-1.save")).write_bytes(b"x")
    monkeypatch.setenv("TR_SAVES_DIR", root)
    monkeypatch.setenv("TR_SINGLE_FILE", "0")
    r = TestClient(app).get("/api/saves")
    assert r.status_code == 200
    ids = {s["slot_id"] for s in r.json()}
    assert ids == {"File_1", "File_2"}
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)
    monkeypatch.delenv("TR_SINGLE_FILE", raising=False)


def test_cheat_seed_without_bridge_returns_503(client):
    # No bridge in test env, should 503 with helpful message
    with patch("planner.server.app._try_bridge", return_value=None):
        r = client.post("/api/cheat/seed", json={"itemId": 9999, "count": 5})
        # either 503 bridge error or 500 if item invalid — but should mention bridge
        assert r.status_code in (400, 503)
        if r.status_code == 503:
            assert "bridge" in r.text.lower()


def test_shop_buy_requires_bridge(client):
    with patch("planner.server.app._try_bridge", return_value=None):
        r = client.post("/api/shop/buy", json={"itemId": 100, "count": 1, "price": 100})
        assert r.status_code == 503
        assert "bridge" in r.text.lower()


def test_shop_sell_requires_bridge(client):
    with patch("planner.server.app._try_bridge", return_value=None):
        r = client.post("/api/shop/sell", json={"itemId": 100, "count": 1, "price": 50})
        assert r.status_code == 503


def test_cheat_money_invalid_payload(client):
    r = client.post("/api/cheat/money", json={"copper": "not_an_int"})
    assert r.status_code in (400, 404, 500, 503)


def test_websocket_bridge_event_shape_via_push(client):
    # Ensure push payload shape matches what web UI expects: type bridge_event
    # We can't open WS in TestClient easily, but we can verify broadcast path doesn't crash
    for payload in [
        {"type": "addItem", "data": {"itemId": 412}},
        {"type": "addMoney", "data": {"copper": 10000}},
        {"event": {"type": "shop/buy", "data": {"itemId": 101, "count": 2}}},
    ]:
        r = client.post("/api/bridge/push", json=payload)
        assert r.status_code == 200


def test_api_saves_structure(client, monkeypatch, tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "File_1"))
    pathlib.Path(os.path.join(root, "File_1", "SaveFile-1.save")).write_bytes(b"fake")
    monkeypatch.setenv("TR_SAVES_DIR", root)
    r = TestClient(app).get("/api/saves")
    assert r.status_code == 200
    s = r.json()[0]
    assert "slot_id" in s
    assert "label" in s
    assert "mtime" in s
    assert "latest_file" in s
    assert s["latest_file"].endswith(".save")
    monkeypatch.delenv("TR_SAVES_DIR", raising=False)
