# Gap Analysis: the perfect vision vs TRP reality

Companion to [`HOW_TO_REPLICATE_FOR_ANOTHER_GAME.md`](HOW_TO_REPLICATE_FOR_ANOTHER_GAME.md)
(the "perfect vision"). This document holds the current implementation
accountable to that vision: each entry is an issue-ready gap — **ID, vision,
current, impact, effort, depends-on, affected files** — to be expanded into
GitHub issues and fixed. Dated 2026-09-01; update statuses as work lands.

Effort scale: **S** hours, **M** days, **L** week+.

---

## 1. The component contract (roles each part must own)

| Component | Role | Owns | Must NOT |
|---|---|---|---|
| **Planner** (FastAPI + web UI) | Knowledge base + state owner. Knows all data & game assets via the startup dump scripts. Computes every diff. Robust, never fragile. | Save parsing (read-only), catalog/assets, plan engine, live-vs-save diff, UI, mode state | Mutate the game through the save file; duplicate bridge responsibilities; crash when the bridge/game disappears |
| **Bridge** (BepInEx plugin, in-game) | Thin live-state reporter + the ONLY mutation channel into the running game. Raw IDs/counts only. | Heartbeat, verified live reads, mutations with before/after verification, event push, planner process spawn/supervision | Duplicate item names/catalog/asset knowledge (IDs only); act as source of truth |
| **Save file** | Durable truth, written by the game alone | Persistence checkpoints | Being written to by the planner (read-only to the planner) |

**Design invariants:**
1. Never mutate the game through the save file. The bridge is the only write
   channel to live state.
2. The bridge reports raw IDs and counts; the planner resolves them to names
   via its own extracted catalog. (Verified already true today.)
3. Every mutation returns the verified value **before** and **after** the
   change — never trust `"ok"` alone.
4. The planner is the non-fragile side: heartbeat loss degrades it to
   save-only read mode; it never needs the bridge to survive.

## 2. The lifecycle & truth model

The bridge lives inside the game process, so it inherently knows whether the
game is up — it (not the planner) is the liveness authority.

**Launch modes:**
1. **Open the game → bridge spawns the planner** → LIVE mode from birth.
   Bridge is parent; owns supervision (restart planner if it dies).
2. **Open the planner standalone (game not running)** → SAVE-ONLY read mode.
   The save file is the most current game data. Normal state, no warning.
3. **Game already running, planner opened standalone** → planner sees the
   bridge heartbeat arriving and promotes itself to LIVE.

**Liveness protocol (heartbeat):** the bridge posts a periodic heartbeat to
the planner (`:8765`); the planner derives "live" purely from heartbeat
presence — first heartbeat → live; heartbeat timeout → drop to save-only
gracefully, with a **reason** (`live` / `no_bridge` / `beat_lost`) so the UI
stays quiet about "game closed" but loud about a lost beat (see DEG-1).
Implemented — see G0.

**State matrix:**

| Game | Bridge/heartbeat | Planner mode | UI shows |
|---|---|---|---|
| down | absent | save-only (normal) | "saved as of HH:MM" |
| up | up | live | green ● live |
| up | down (bridge crashed) | error/degraded | loud flag — bridge should be up whenever the game is |

---

## 3. Resolved gaps (fixed 2026-09-01)

### MUT-1 — Planner mutated the game via save-file byte-patching ✅
- **Was:** `/api/cheat/money` and `/api/cheat/seed` fell back to binary-patching
  the `.save` (and `.backup`) via NRBF-offset instrumentation
  (`_locate_money_offset`, `_locate_inventory_offset`) when the bridge was
  offline — ~230 lines of version-sensitive mutation code that violated the
  role contract.
- **Root cause of the failing live-seed test:** the planner patched the save
  on disk instead of going through the bridge, so the *running game* never
  saw the change and the verified read-back failed.
- **Fix:** planner commit `308fe1a` — removed both fallbacks and the dead
  helpers; all four write endpoints (`cheat/money`, `cheat/seed`,
  `shop/buy`, `shop/sell`) now uniformly refuse with 503
  *"game not running — PlannerBridge offline; cannot mutate live state."*
- **Reproduce (historical):** with the game+bridge running,
  `pytest tests/test_live_seed_happy_path.py -v -s` — the happy path added a
  seed through the planner, then failed the live read-back because the
  mutation diverted to the save-patch path. Now the only path is
  planner → bridge → game, with verified before/after (MUT-2).

### MUT-2 — Mutations returned no verified before/after; no targeted read ✅ (bridge side)
- **Was:** bridge mutation endpoints returned only `{"ok":true,...}` — no
  before/after state — and the only live read was the full `/debug/inventory`
  snapshot.
- **Fix:** bridge commit `7263cd4` — every mutation
  (`addItem`, `addMoney`, `shop/buy`, `shop/sell`) now reads the verified live
  state immediately before and after the change (same serialized block) and
  returns `before`/`after` in both the HTTP response and the pushed
  `bridge_event`; `-1` = could not verify. New `GET /value?itemId=N[&money=1]`
  targeted verified read ("how many of X right now?").
- **Still open:** surfacing the verified before/after in mutation *responses*
  beyond the toast (e.g. cheat panel confirming the new balance) — folded
  into LV-1 (provenance surfacing). The toast path itself is done (EV-1).

---

### EV-1 — `bridge_event` broadcasts were dropped by the frontend ✅
- **Was:** backend rebroadcasts `bridge_event` (`/api/bridge/push` → `/ws`),
  but the single-file UI's `connectWS` in `static.py` handled only
  `save_changed`/`cart_updated`/`menu_updated` — every `bridge_event` was
  silently ignored. (The React UI already consumed it, but rendered raw JSON.)
- **Fix:** single-file UI now has a ledger-themed toast stack
  (`#bridgeToasts`): every bridge event renders a human label using the
  verified before/after contract (`+5 × #123 (2 → 7)`,
  `bought 3 × #45 (item 2 → 12 · 1.00g → 0.90g)`, errors in burgundy), then
  refreshes at 250 ms to merge live counts ahead of the save flush. The React
  UI's toast was upgraded to the same before/after labels, its stale
  "save-patch (needs Load)" cheat label was corrected to the bridge-only
  refusal behavior, and four malformed JSX comments that broke `tsc -b`
  were fixed.

### SEC-1 — `SHARE_TOKEN` was generated but never enforced ✅
- **Was:** `SHARE_TOKEN = _secrets.token_urlsafe(16)` at `app.py` was
  generated and never referenced; CORS allow-list was the only guard, so a
  public share/tunnel link allowed anyone to mutate the host's game
  (cheat/buy/sell) and to spoof `bridge_event` pushes.
- **Fix:** share-mode (`--share`/`--tunnel` → `TR_SHARE=1`) token gate:
  - Game-mutating endpoints (`/api/cheat/*`, `/api/shop/*`) require the
    per-run token (`X-Share-Token` header or `?token=`), compared with
    `secrets.compare_digest`. 401 otherwise.
  - The host's own direct-localhost browser is exempt (frictionless);
    ngrok-proxied traffic is NOT exempt (loopback + `X-Forwarded-For`
    ⇒ treated as remote).
  - `/api/bridge/push` is local-only in share mode (403 for
    remote/tunneled callers — anti-spoof; the bridge never leaves the
    machine). Default mode is unchanged (bound to `127.0.0.1`).
  - Share URLs now carry the token: `http://ip:port/#t=<token>` — printed
    by `--share`/`--tunnel` startup. The React UI extracts `#t=`/`?token=`
    and sends `X-Share-Token` on all four write calls.
  - Regression tests: `tests/test_share_token.py` (default mode open,
    401 without/with-wrong token, pass with header/query token,
    bridge-push 403 remote, 200 default).

### G0 — Bridge-owned lifecycle: heartbeat + planner spawn ✅
- **Was:** planner and bridge were independent processes that found each
  other via opportunistic per-request HTTP polls; no heartbeat existed on
  either side; no spawn/supervision; no mode concept.
- **Fix (bridge `Plugin.cs` 1.2.0):**
  - **Heartbeat thread** POSTs to the planner's
    `/api/bridge/heartbeat` every 2s (configurable via
    `BepInEx/config/plannerbridge.cfg` `Lifecycle.HeartbeatIntervalMs`),
    quiet — no event spam; a final `stopping:true` beat on game quit lets
    the planner drop to save-only instantly.
  - **Launch mode 1:** on startup, if no planner answers on `:8765`, the
    bridge spawns it (`bash start_planner.sh`, detached;
    `Lifecycle.SpawnPlanner` + `Lifecycle.PlannerDir` config).
  - **Supervision:** if a planner it owns stops answering (5 missed beats),
    it restarts it — max 3 tries, 30s cooldown.
  - Launch modes 2/3 need no code: a standalone planner simply starts
    receiving beats and promotes itself.
- **Fix (planner `app.py`):** heartbeat listener records the last beat;
  `_bridge_live()` derives live mode purely from freshness (6s timeout ≈ 3
  missed beats); `_live_status_watcher` broadcasts `live_status` on `/ws`
  within ~1s of every transition; `/api/bridge/status` now carries
  `live`/`heartbeat_age_s`/`heartbeat_timeout_s` (and still returns them on
  503 when the bridge HTTP is down). Local-only in share mode (403 remote).
- **Verified:** planner side proven end-to-end **without the game** via the
  bridge simulator (`tests/mock_bridge.py` — the bridge is just an HTTP
  contract, so a stdlib Python sim of Plugin.cs 1.2.0 covers it):
  `tests/test_heartbeat.py` + `tests/test_mock_bridge_integration.py`
  (103 passing) include a full-stack test — real uvicorn planner + sim
  heartbeats — proving live flips ON from beats and OFF when they stop even
  while the sim's HTTP still answers (heartbeat is the mode authority, not
  reachability). Manual sim: `.venv/bin/python -m tests.mock_bridge`.
  New env overrides: `TR_BRIDGE_BASE` (point the planner at any bridge URL,
  e.g. the sim), `TR_HEARTBEAT_TIMEOUT` (shorten for tests).
  **Still pending: in-game behavior of the 1.2.0 DLL itself** (spawn config
  path, heartbeat cadence under real load) — needs one run with the game.
- **Known limitation carried to DEG-1:** heartbeat loss can't distinguish
  "game closed" from "bridge crashed while game up" — both read as
  save-only.

### G1 — Planner mode concept + `live_status` over the WebSocket ✅
- **Was:** no mode anywhere; `/ws` carried only `save_changed`,
  `cart_updated`, `menu_updated`, `bridge_event`; `#ws-pill` tracked only the
  websocket connection itself.
- **Fix:** mode is heartbeat-derived (G0); `live_status` broadcast on every
  flip; single-file UI got a `#live-pill` next to `#ws-pill`
  (green *live* / quiet *save-only* — game-closed is the NORMAL state, so
  no alarm colour; initial state fetched at boot); React UI flips
  `bridgeLive` instantly from the WS message and its `/api/bridge/status`
  poll now prefers the heartbeat `live` field (also kept on 503 payloads)
  over raw proxy success.

### DEG-1 — Degraded state not surfaced (bridge down while game up) ✅ (accepted-merge variant)
- **Was:** silently fell back to save data regardless of why the bridge was
  absent; user was never told anything.
- **Fix:** `live_status` and `/api/bridge/status` now carry a **reason**:
  `live` (beats fresh), `no_bridge` (never saw a beat — game closed, the
  NORMAL state, UI stays quiet "save-only"), `beat_lost` (we HAD beats and
  they stopped — amber "bridge lost" pill + toast: *"save-only mode. If the
  game is still running, check BepInEx/LogOutput.log"*). Single-file UI:
  amber `.ws-pill.lost` state; React: badge text + error toast.
- **Verified in-browser** against the simulator: badge flips
  "bridge live · 2 req" → "bridge lost — check BepInEx log" with the toast
  on beat loss; tests assert the no_bridge → live → beat_lost reason
  transitions.
- **Inherent limitation (accepted):** a crashed bridge cannot report
  anything, so heartbeat loss alone can't distinguish "game closed" from
  "bridge crashed while game up" — `beat_lost` covers both with a hint
  pointing at the BepInEx log. True row-3 detection would need an
  independent game-process signal (e.g. a supervisor outside the game
  process); deferred unless it proves annoying in practice.

## 4. Open gaps (issue-ready)

### LV-1 — Live-vs-save provenance only on inventory; UI never renders it
- **Vision:** every number that differs between live and save carries a
  source badge (live / saved-as-of / ⚠ not saved yet, `save_count` tooltip).
- **Current:** `live`/`changed`/`slot_live`/`live_available` are emitted only
  by `/api/state` + `/api/inventory/grouped` (`app.py:~386-427`); no
  provenance on `/api/plan` etc.; frontend renders no source badges (all
  `.badge` CSS is gameplay badges).
- **Effort:** **L** (thread provenance model → plan → `plan_to_dict` → UI).
  **Depends-on:** G0, G1 (mode classification drives the badge).
- **Files:** `planner/server/app.py`, `planner/plan/engine.py`,
  `planner/server/static.py`.

### SLS-1 — "Since last save" tracker absent (the feature that justifies live mode)
- **Vision:** snapshot `GameState` on every `save_changed`; accumulate bridge
  deltas (now carrying verified before/after) between saves; render "things
  completed since your last save." High value because TR only persists on
  sleep — quitting mid-day loses a session of progress this would surface.
- **Current:** nothing exists — no baseline snapshot, no delta log, no
  endpoint, no UI.
- **Effort:** **L**. **Depends-on:** G0, G1, EV-1 (deltas arrive as bridge
  events).
- **Files:** `planner/server/app.py` (new), `planner/server/static.py`.

### TST-1 — No tests for the new contracts
- **Vision:** tests for heartbeat/mode transitions, before/after verification
  pass-through, `bridge_event` routing, `live_status` broadcast, token gate —
  each auto-skipping when the game/bridge is absent (pattern already used by
  `test_live_seed_happy_path.py`).
- **Current:** 103 passing tests. Covered: catalog/currency/parser/math/API,
  bridge realtime + rebroadcast, SEC-1 token gate
  (`test_share_token.py`), heartbeat mode flips + full planner↔sim
  integration with verified before/after and the 503 offline refusal
  (`test_heartbeat.py`, `test_mock_bridge_integration.py` against the
  `tests/mock_bridge.py` simulator — no game needed). Remaining: `live_status`
  WS broadcast assertion (needs a WS client test), and updating
  `test_live_seed_happy_path.py` for the before/after shape with the real
  game+bridge (the one thing the simulator can't prove — in-game DLL
  behavior).
- **Effort:** **S/M**. **Depends-on:** G0 ✅, G1 ✅.
- **Files:** `tests/` (new), `tests/test_live_seed_happy_path.py` (update).

---

## 5. Suggested triage order

1. ~~**EV-1** (S) — quick win; makes MUT-2's before/after visible in the UI.~~ ✅ done
2. ~~**SEC-1** (M) — sharing is exposed today.~~ ✅ done
3. ~~**G0** (L) — heartbeat + bridge-spawns-planner.~~ ✅ done (planner side proven via simulator; one in-game DLL check pending)
4. ~~**G1** (M) — mode + `live_status` badge.~~ ✅ done
5. ~~**DEG-1** — beat_lost vs no_bridge distinction + hint.~~ ✅ done (accepted-merge variant; true row-3 detection deferred)
6. **LV-1** (L) — provenance badges everywhere.
7. **SLS-1** (L) — since-last-save tracker.
8. **TST-1** (S/M) — WS broadcast test + live happy-path update with the game.
