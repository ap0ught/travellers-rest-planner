"""Live happy-path test: add a random seed through the live BepInEx bridge and
verify it appears in the live inventory read-back.

This is NOT a simulated/static test. It exercises the real, in-game pipeline:

    test -> planner http://127.0.0.1:8765/api/cheat/seed
         -> BepInEx bridge http://127.0.0.1:8766/addItem
         -> running Travellers Rest game adds N of the seed
    test <- planner /api/inventory_grouped (merges live bridge inventory)

Usage of "12":
  - random.seed(12) makes the seed pick deterministic (repeatable run id).
  - count = 12 is the amount added, used to confirm THIS test's submission.

Because this is a live diagnostic, it is skipped (not failed) when the planner,
bridge, or running game is unavailable so it never breaks normal/CI runs.

When the happy path fails, targeted diagnostics point at the code that needs
fixing (see assert messages). Known broken pieces of the happy path (as of the
bridge 1.1.0 snapshot) are documented below.

Known bridge issues surfaced by this test:
  * BepInEx LogOutput.log shows `AddItem <id> x12 syncResult=timeout (elapsed
    2501ms)` — the in-game grant runs on the main thread and times out before
    the bridge reports a sync result, so nothing is actually added.
  * Bridge `/debug/inventory` returns only low item ids (1..47) with stack 0
    and never reflects owned/live-added seeds, so the live read-back via
    `_fetch_live_bridge_counts` does not currently show the added seed.

To run (start_planner.sh + game with PlannerBridge running):
    .venv/bin/python -m pytest tests/test_live_seed_happy_path.py -v -s
"""
import json
import random
import time
import urllib.error
import urllib.request

import pytest

from planner.catalog import load_catalog

LIVE_PLANNER = "http://127.0.0.1:8765"
LIVE_BRIDGE = "http://127.0.0.1:8766"
ADD_COUNT = 12          # number of the seed this test submits (the identifier)
RNG_SEED = 12           # fixed RNG seed so the picked seed is repeatable
POLL_TIMEOUT_S = 15.0
POLL_INTERVAL_S = 0.5


def _http(verb: str, url: str, payload: dict | None = None, timeout: float = 8.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=verb
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _planner_up() -> bool:
    try:
        _http("GET", f"{LIVE_PLANNER}/api/bridge/status", timeout=2.0)
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def live_server():
    """Ensure the live planner is reachable; otherwise skip the whole module."""
    if not _planner_up():
        pytest.skip(
            "live planner not running at %s (start start_planner.sh + TR with "
            "PlannerBridge). This is a live happy-path test, skipped in CI." % LIVE_PLANNER
        )
    return LIVE_PLANNER


def _live_count(server: str, item_id: int) -> tuple[int, bool]:
    """Read count of item_id through the planner (merging live bridge counts).

    Returns (count, is_live). Uses /api/inventory_grouped so the read passes
    through the BepInEx live inventory when the bridge exposes it.
    """
    data = _http("GET", f"{server}/api/inventory/grouped")
    is_live = bool(data.get("live_available"))
    total = 0
    for entries in data.get("groups", {}).values():
        for e in entries:
            if int(e.get("item_id")) == int(item_id):
                total = int(e.get("count", 0))
    return total, is_live


def test_happy_path_add_random_seed_live(live_server):
    """Happy path: pick a random seed (deterministic via seed=12), add 12 of it
    through the BepInEx bridge, then read it back live and confirm +12."""
    # 1) Deterministically pick a random seed item from the catalog.
    random.seed(RNG_SEED)
    seeds = [it for it in load_catalog().items_by_id.values() if it.item_id >= 3000]
    assert seeds, "catalog has no seed items (ids >= 3000) to pick from"
    pick = random.choice(seeds)
    item_id = pick.item_id
    print(f"\n[happy-path] picked seed item {item_id} ({pick.name}) via random.seed({RNG_SEED})")

    # 2) Baseline live count BEFORE adding.
    before, live = _live_count(live_server, item_id)
    print(f"[happy-path] baseline live count of {item_id} = {before} (live={live})")

    # 3) Add ADD_COUNT of it via /api/cheat/seed -> BepInEx /addItem -> game.
    r = _http("POST", f"{live_server}/api/cheat/seed",
              {"itemId": item_id, "count": ADD_COUNT}, timeout=20.0)
    print(f"[happy-path] POST /api/cheat/seed -> ok={r.get('ok')} bridge={r.get('bridge')} "
          f"realtime={r.get('realtime')} result={r.get('result')}")
    assert r.get("ok") is True, (
        f"add did not return ok: {r}. This typically means the BepInEx bridge "
        f"addItem timed out (LogOutput.log: 'syncResult=timeout'). Fix the bridge's "
        f"SyncOnMainThread addItem so it reports a real result instead of timing out."
    )

    # 4) Read back live until the count reflects the +12 submission.
    expected = before + ADD_COUNT
    deadline = time.time() + POLL_TIMEOUT_S
    observed = None
    while time.time() < deadline:
        observed, live = _live_count(live_server, item_id)
        if observed == expected:
            break
        time.sleep(POLL_INTERVAL_S)

    assert observed == expected, (
        f"happy path failed: added {ADD_COUNT} of item {item_id} but live read-back "
        f"shows {observed} (expected {expected}, baseline {before}, live={live}). "
        f"This means the add did not land and/or the live read via the bridge is "
        f"missing it. Fixes needed:\n"
        f"  (a) BepInEx bridge /addItem returns 'syncResult=timeout' (elapsed 2501ms) "
        f"and never actually grants the item.\n"
        f"  (b) BepInEx bridge /debug/inventory returns only low ids 1..47 with "
        f"stack 0 and never includes owned/added seeds, so _fetch_live_bridge_counts "
        f"in planner/server/app.py cannot observe the added seed. "
        f"Make /debug/inventory reflect the real item inventory."
    )
    print(f"[happy-path] SUCCESS: item {item_id} live count is now {observed} "
          f"(was {before}, added {ADD_COUNT}, live={live})")


def test_bridge_reports_add_item_without_timeout(live_server):
    """Diagnostic: the bridge's addItem RPC should return a completed sync result,
    not 'timeout'. 12 of a deterministic seed (=12) is added to observe the result."""
    random.seed(RNG_SEED)
    seeds = [it for it in load_catalog().items_by_id.values() if it.item_id >= 3000]
    pick = random.choice(seeds)

    # Direct call to the bridge, bypassing the planner, to isolate addItem behavior.
    payload = {"itemId": pick.item_id, "count": ADD_COUNT}
    try:
        r = _http("POST", f"{LIVE_BRIDGE}/addItem", payload, timeout=15.0)
    except urllib.error.HTTPError as he:
        r = {"_http_status": he.code, "body": he.read().decode()[:200]}
    print(f"\n[diag] bridge /addItem({pick.item_id}, {ADD_COUNT}) -> {r}")
    sync = r.get("syncResult") or r.get("result")
    assert r.get("ok") is not False and "timeout" not in str(sync).lower(), (
        f"bridge addItem reported a timeout ({sync}). The in-game grant is not "
        f"completing. Fix the bridge change: AddItem/SyncOnMainThread must return "
        f"the actual grant result instead of timing out at ~2500ms."
    )
