"""Standalone PlannerBridge simulator — no game needed.

The bridge is *just an HTTP contract* (Plugin.cs 1.2.0): raw IDs/counts,
verified before/after on every mutation, and a liveness heartbeat. This
module implements that contract against an in-memory game state so the whole
planner <-> bridge pipeline — heartbeat -> live mode, cheat/buy/sell with
verified before/after, targeted live reads — can be developed, tested, and
demonstrated without running Travellers Rest.

Manual run (planner picks it up on the default port):
    .venv/bin/python -m tests.mock_bridge                # :8766 + heartbeat
    .venv/bin/python -m tests.mock_bridge --port 8767    # custom port
    .venv/bin/python -m tests.mock_bridge --no-heartbeat

In tests:
    from tests.mock_bridge import MockBridge
    mb = MockBridge(port=0, heartbeat=False).start()     # ephemeral port
    os.environ["TR_BRIDGE_BASE"] = mb.url               # point planner at it
    ...
    mb.stop()
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIM_VERSION = "1.2.0-sim"


class _SimState:
    """The 'running game': an inventory of raw {itemId: stack} + copper."""

    def __init__(self, inventory: dict[int, int] | None = None, copper: int = 10_000):
        self.inventory: dict[int, int] = dict(inventory or {})
        self.copper = copper
        self.events: list[dict] = []
        self.requests = 0
        self.started_at = time.time()
        self.lock = threading.Lock()

    def count(self, item_id: int) -> int:
        return self.inventory.get(item_id, 0)

    def push_event(self, ev_type: str, data: dict) -> None:
        with self.lock:
            self.events.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "type": ev_type, "data": data})
            del self.events[:-100]  # ring of 100 like MaxEvents


def _parse_int(source: dict, *names: str, default: int = 0) -> int:
    for n in names:
        if n in source:
            try:
                return int(source[n])
            except (TypeError, ValueError):
                pass
    return default


def make_handler(state: _SimState, planner_url: str | None, push_events: bool):
    """Build a BaseHTTPRequestHandler closed over the sim state."""

    def notify_planner(ev: dict) -> None:
        # Mirror Plugin.cs NotifyPlannerApp: POST bridge events to the planner
        # so the web UI can toast/pulse in <200ms.
        if not planner_url:
            return
        try:
            body = json.dumps({"type": "bridge_event", "event": ev}).encode()
            req = urllib.request.Request(
                planner_url.rstrip("/") + "/api/bridge/push",
                data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=0.8) as r:
                r.read()
        except Exception:
            pass  # planner not running (yet) — same as the real bridge

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # quiet
            pass

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return {}

        def do_OPTIONS(self):  # CORS preflight like the real bridge
            self._json(204, {})

        def do_GET(self):
            state.requests += 1
            path = self.path.split("?")[0]
            qs = {}
            if "?" in self.path:
                qs = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p)

            if path == "/ping":
                self._json(200, {"ok": True, "sim": True})
            elif path in ("/status", "/bridge/status"):
                self._json(200, {"ok": True, "bridge": "planner", "sim": True,
                                 "version": SIM_VERSION,
                                 "uptime_s": round(time.time() - state.started_at, 1),
                                 "requests": state.requests, "single_file": True,
                                 "verbose": False})
            elif path in ("/events", "/bridge/events"):
                with state.lock:
                    self._json(200, {"events": list(state.events)})
            elif path in ("/bridge/inventory", "/debug/inventory"):
                items = [{"itemId": iid, "stack": n, "field": "all"}
                         for iid, n in sorted(state.inventory.items()) if n > 0]
                self._json(200, {"ok": True, "items": items, "copper": state.copper,
                                 "count": 1 if items else 0})
            elif path in ("/bridge/state", "/debug/state"):
                self._json(200, {"ok": True, "copper": state.copper,
                                 "uptime_s": round(time.time() - state.started_at, 1),
                                 "requests": state.requests})
            elif path in ("/value", "/bridge/value"):
                item_id = _parse_int(qs, "itemId", "item_id", "id")
                if not item_id:
                    self._json(400, {"error": "itemId required", "hint": "/value?itemId=123[&money=1]"})
                    return
                want_money = qs.get("money") == "1"
                self._json(200, {"ok": True, "itemId": item_id,
                                 "count": state.count(item_id),
                                 "money": state.copper if want_money else -1})
            else:
                self._json(404, {"error": "not found",
                                 "try": ["/ping", "/bridge/status", "/bridge/events",
                                         "/debug/state", "/addItem", "/addMoney",
                                         "/shop/buy", "/shop/sell", "/value"]})

        def do_POST(self):
            state.requests += 1
            path = self.path.split("?")[0]
            qs = {}
            if "?" in self.path:
                qs = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&") if "=" in p)
            body = self._read_body()
            merged = {**body, **qs}  # query params win, like the real bridge

            if path in ("/addItem", "/addSeed"):
                item_id = _parse_int(merged, "itemId", "item_id", "seedId")
                count = _parse_int(merged, "count", "amount", "qty", default=1)
                if not item_id:
                    self._json(400, {"error": "itemId required", "hint": "POST {itemId, count}"})
                    return
                count = max(1, min(999, count))
                before = state.count(item_id)
                state.inventory[item_id] = before + count
                after = state.count(item_id)
                ev = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "type": "addItem",
                      "data": {"itemId": item_id, "count": count, "before": before,
                               "after": after, "result": "ok", "elapsed_ms": 1}}
                state.push_event("addItem", ev["data"])
                if push_events:
                    threading.Thread(target=notify_planner, args=(ev,), daemon=True).start()
                self._json(200, {"ok": True, "itemId": item_id, "count": count,
                                 "before": before, "after": after,
                                 "result": "ok", "elapsed_ms": 1})

            elif path == "/addMoney":
                copper = _parse_int(merged, "copper", "amount")
                action = str(merged.get("action", "add"))
                if copper == 0:
                    copper = 50_000  # the real bridge's default
                before = state.copper
                if action == "set":
                    copper = copper - before  # set -> delta, like the bridge
                state.copper = max(0, state.copper + copper)
                after = state.copper
                state.push_event("addMoney", {"copper": copper, "action": action,
                                              "before": before, "after": after,
                                              "result": "ok", "elapsed_ms": 1})
                self._json(200, {"ok": True, "copper": copper, "action": action,
                                 "before": before, "after": after, "result": "ok"})

            elif path == "/shop/buy":
                item_id = _parse_int(merged, "itemId", "item_id")
                count = _parse_int(merged, "count", default=1)
                price = _parse_int(merged, "price", "buy_copper")
                before_money, before_item = state.copper, state.count(item_id)
                if not item_id:
                    self._json(400, {"error": "itemId required"})
                    return
                if state.copper < price:
                    self._json(500, {"ok": False, "error": f"need {price} have {state.copper}",
                                     "itemId": item_id})
                    return
                state.copper -= price
                state.inventory[item_id] = before_item + count
                after_money, after_item = state.copper, state.count(item_id)
                state.push_event("shop/buy", {"itemId": item_id, "count": count, "price": price,
                                              "before_money": before_money, "after_money": after_money,
                                              "before_item": before_item, "after_item": after_item,
                                              "result": "ok", "elapsed_ms": 1})
                self._json(200, {"ok": True, "itemId": item_id, "count": count, "price": price,
                                 "before": {"money": before_money, "item": before_item},
                                 "after": {"money": after_money, "item": after_item},
                                 "result": "ok"})

            elif path == "/shop/sell":
                item_id = _parse_int(merged, "itemId", "item_id")
                count = _parse_int(merged, "count", default=1)
                price = _parse_int(merged, "price", "sell_copper")
                if not item_id:
                    self._json(400, {"error": "itemId required"})
                    return
                before_money, before_item = state.copper, state.count(item_id)
                state.inventory[item_id] = max(0, before_item - count)
                state.copper += price
                after_money, after_item = state.copper, state.count(item_id)
                state.push_event("shop/sell", {"itemId": item_id, "count": count, "price": price,
                                               "before_money": before_money, "after_money": after_money,
                                               "before_item": before_item, "after_item": after_item,
                                               "result": "ok", "elapsed_ms": 1})
                self._json(200, {"ok": True, "itemId": item_id, "count": count, "price": price,
                                 "before": {"money": before_money, "item": before_item},
                                 "after": {"money": after_money, "item": after_item},
                                 "result": "ok"})

            else:
                self._json(404, {"error": "not found"})

    return Handler


class MockBridge:
    """Runnable bridge simulator on its own threads.

    port=0 binds an ephemeral port (see .url). heartbeat=True starts the G0
    liveness loop POSTing to the planner, exactly like Plugin.cs 1.2.0.
    """

    def __init__(self, port: int = 8766, heartbeat: bool = True,
                 heartbeat_interval: float = 0.5,
                 planner_url: str = "http://127.0.0.1:8765",
                 inventory: dict[int, int] | None = None, copper: int = 10_000,
                 push_events: bool = True):
        self.state = _SimState(inventory=inventory, copper=copper)
        self._server = ThreadingHTTPServer(("127.0.0.1", port),
                                           make_handler(self.state, planner_url, push_events))
        self._heartbeat = heartbeat
        self._heartbeat_interval = heartbeat_interval
        self._planner_url = planner_url
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "MockBridge":
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="MockBridge-HTTP").start()
        if self._heartbeat:
            self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True,
                                               name="MockBridge-Heartbeat")
            self._hb_thread.start()
        return self

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()

    def stop(self) -> None:
        self.stop_heartbeat()
        self._server.shutdown()
        self._server.server_close()

    def _heartbeat_loop(self) -> None:
        # Mirror Plugin.cs HeartbeatLoop: quiet fixed ping, no event spam.
        while not self._hb_stop.is_set():
            try:
                body = json.dumps({
                    "type": "heartbeat", "bridge": "planner", "version": SIM_VERSION,
                    "uptime_s": round(time.time() - self.state.started_at, 1),
                    "game_running": True, "stopping": False,
                    "spawned_planner": False, "planner_restarts": 0,
                }).encode()
                req = urllib.request.Request(
                    self._planner_url.rstrip("/") + "/api/bridge/heartbeat",
                    data=body, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=0.9) as r:
                    r.read()
            except Exception:
                pass  # planner not running — same as the real bridge
            self._hb_stop.wait(self._heartbeat_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="PlannerBridge simulator (no game)")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--planner", default="http://127.0.0.1:8765",
                        help="planner base URL for heartbeat + event push")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="heartbeat interval seconds")
    parser.add_argument("--no-heartbeat", action="store_true")
    parser.add_argument("--copper", type=int, default=10_000)
    args = parser.parse_args()

    sim = MockBridge(port=args.port, heartbeat=not args.no_heartbeat,
                     heartbeat_interval=args.interval, planner_url=args.planner,
                     copper=args.copper).start()
    print(f"[mock-bridge] {SIM_VERSION} on {sim.url} "
          f"(heartbeat {'ON' if not args.no_heartbeat else 'OFF'} -> {args.planner})")
    print("[mock-bridge] Ctrl+C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sim.stop()
        print("[mock-bridge] stopped")


if __name__ == "__main__":
    main()
