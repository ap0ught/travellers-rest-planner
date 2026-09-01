# How to replicate this project for another game + save file

**Purpose:** This document serves **two jobs**:

1. **An accurate product/flow spec** — a precise description of *how the whole
   system flows* (data sources → extraction → live updates → UI) and *what the
   product is*, so you can reason about the design, agree on scope, and see how
   the pieces fit before (or without) writing code.
2. **A build playbook** — the repeatable, step-by-step engineering process for
   building a Travellers-Rest-style planner ("live save reader + game-data
   research wiki web app") for **any** other game and its save file, distilling
   everything learned building `travellers-rest-planner/`.

It is **not** just a feature list (that's [`../README.md`](../README.md)). The
value is the *flow*: what to reverse-engineer, in what order, which tools to
use, what to build, how the two live-update mechanisms combine, and — most
importantly — the **gotchas** that took the longest to figure out.

---

## 1. The big picture (the mental model / the product)

This whole project is three data pipelines that meet in a web UI, driven by
**two live-update paths** that work together:

```
                        GAME ASSETS                     RUNNING GAME
                   (items, recipes, maps,     (in-game events, live counts)
                    i18n, icons, quests)              │
                        │                             │ BepInEx bridge (8766)
                        │ scripts/dump_*              ▼
                        ▼                        bridge events / live API
               data/ + dumps/                        │
                   (JSON/CSV/PNG)                    │
                        │                            │
                        └──────────────┐  ┌─────────┘
                                       ▼  ▼
                          planner/catalog.py (join save-IDs ↔ extracted data)
                                       │
                                       ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  planner/server/app.py — FastAPI + ONE WebSocket (/ws)        │
   │                                                               │
   │   watchdog path:  save autosaves ─▶ save_changed ─▶ re-parse  │
   │                   save  → authoritative full GameState        │
   │                                                               │
   │   bridge path:    in-game event ─▶ bridge_event/live counts   │
   │                   → fast hint (<200ms), merged over the save  │
   └───────────────────────┬───────────────────────────────────────┘
                           ▼
              planner/server/static.py  <-- THE UI (single-file HTML/JS)
                 · every value tagged live-vs-save (source badge)
                 · "since last save" delta banner (TR saves on sleep)
```

Three pipelines + two live paths:

1. **A static catalog** — extracted one-time from the game's asset bundles
   (recipes, items, crops, quests, maps, icons, translations). This is the
   "wiki" half.
2. **A live save reader (watchdog)** — watches the save folder, parses the
   game's save format on every autosave, and extracts the player's current
   state (money, date, planted crops, unlocked recipes, trends, quests…). This
   is the **authoritative** view.
3. **An in-game bridge (optional accelerator)** — reads live in-game state /
   events near-instantly and feeds a *fast hint* that overlays the save view
   (so buy/sell shows before the save flushes) and powers the **"since last
   save"** tracker.
4. **A web app** — joins all of the above: shows both "what the game contains"
   and "what the player should do *right now*", with every number honest about
   whether it came from live gameplay or the last save.

The two hardest, most game-specific parts are the **save format** (pipeline 2)
and the **insight computation** (the planner math). Everything else is fairly
mechanical once you have a catalog extractor. See §7 for how the two live paths
combine, the live/save badge, and the between-saves tracker.

### The three rules that keep the whole thing maintainable

- **Everything is driven by the game's real field names**, which are more
  stable than method/class names (games are obfuscated; fields usually survive).
- **Every field read is wrapped in try/except** and defaults to `None`/0 when a
  game update renames it. A broken field never crashes the whole read — you just
  lose that one stat until you update the matching reader/extractor.
- **Extraction is a one-shot script, not runtime code.** You run `scripts/dump_*`
  once (and after each game patch) to write JSON/CSV/PNG into `data/` and
  `dumps/`. The running server only *reads* those files. This keeps the web
  server simple and fast.

---

## 2. What you need before you start

- **The game installed** (Steam is easiest; Steam/Proton location logic is
  handled by [`gamepath.py`](../planner/gamepath.py)).
- **The save file location.** Find where saves live. For Unity games on Windows
  this is usually:
  - `%USERPROFILE%\AppData\LocalLow\<company>\<game>\...`
  - On Linux/Proton: `steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser/AppData/LocalLow/<company>/<game>/...`
- **The game's data folder.** Unity installs keep the game logic in
  `<Game>_Data/Managed/Assembly-CSharp.dll` and the assets in
  `*.assets`, `level*`, `globalgamemanagers` files. This is your extraction
  source.
- **Tooling** (from [`requirements.txt`](../requirements.txt)):
  - `UnityPy` + `TypeTreeGeneratorAPI` — read Unity asset bundles & ScriptableObjects.
  - `pypdn` — parse .NET `BinaryFormatter` (NRBF) saves.
  - `Pillow` — image composition (icons, maps).
  - `FastAPI` + `uvicorn` + `watchdog` — web server + save-folder watching.
  - `ilspycmd` (optional, dev-only) — decompile `Assembly-CSharp.dll` for
    reference. Needs .NET SDK.

### Verify before going further
```bash
# Game data folder (Windows example):
Test-Path "C:\...\TravellersRest_Data\Managed\Assembly-CSharp.dll"   # ~16 MB

# Save files exist:
dir "$env:USERPROFILE\AppData\LocalLow\Louqou\TravellersRest\GameSaves"
```

---

## 3. Phase 1 — Find & map the save format (do this first)

This is the highest-risk, highest-value step. Everything else is graded work;
this one can be a rabbit hole. **Budget most of your time here.**

### Step 1.1 — Identify the serializer (one line of analysis, saves hours)

Open a save file in a hex editor and look at the first bytes:

| First bytes | Serializer | Reader |
|---|---|---|
| `00 01 00 00 00 FF FF FF FF 01 00 00 00 00 00 00...` | **.NET BinaryFormatter** (NRBF) | `pypdn` |
| Verbose type-name string / `Sirenix` markers | **Sirenix Odin** | custom reader (see [`odin.py`](../planner/parser/odin.py)) |
| GZip/JSON/other | custom | custom |

> **Travellers Rest was Odin, then moved to BinaryFormatter.** The repo has
> *both* [`odin.py`](../planner/parser/odin.py) (a hand-written Odin binary
> walker) and the BinaryFormatter path in [`saves.py`](../planner/parser/saves.py).
> Don't assume — inspect the actual bytes of *your* game.

The biggest win: if it's **BinaryFormatter**, `pypdn` deserializes the whole
object graph in one call:
```python
n = NRBF(filename=path)
n.resolveReferences()
root = n.getRoot()          # root object; fields accessible by name
```

### Step 1.2 — Discover the data model (the "schema")

You need to know the shape of the root save object. Two ways:

1. **Decompile the save class** with `ilspycmd` and read the fields of the root
   class (e.g. `SaveData`). This gives you exact field names *and* semantics.
2. **Dump it empirically** — walk the parsed object and print every
   top-level field name + type. Fast, but you lose semantics.

Blend both: decompile to know *what to look for*, then print the live object to
confirm names and types.

### Step 1.3 — Write the "extract to GameState" layer

Create a dataclass that captures only what your planner needs (see
[`GameState` in `saves.py`](../planner/parser/saves.py)):
`money`, `date`, `trends`, `unlocked recipes`, `planted crops`, `quests`,
`inventory counts`, `tavern/player names`.

Key patterns (copy these exactly — they're what make the parser survive game
updates):
```python
def _attr(o, name, default=None):
    return getattr(o, name, default)

# Every field inside try/except; default on any failure:
try:
    season_idx = _enum_int(ct.season) or 0   # enums -> int value
except Exception:
    season_idx = 0
```

### Step 1.4 — The environment-variable escape hatches

Your save-folder finder should support overrides so you're never blocked when
auto-detection misses:
- `TR_SAVES_DIR` / `<GAME>_SAVES` — explicit save root override.
- `TR_GAME_DIR` — explicit game data folder override (used by `install.py`).

---

## 4. Phase 2 — Asset extraction (the "catalog")

Now build the one-shot extractors that turn the game's assets into clean JSON/CSV
tables. Each script reads from the game's data folder and writes to `data/` +
`dumps/`. Keep them **idempotent** (safe to re-run) — users re-run them after
every game patch.

| Script | What it extracts | How |
|---|---|---|
| `dump_mono.py` | ScriptableObjects (items, recipes, crops, shops, quests, perks…) | `UnityPy` + `TypeTreeGenerator` on every `*.assets` |
| `dump_i2l.py` | Localization terms × languages | Hand-walk the `LanguageSourceAsset` MonoBehaviours (typetree out-of-bounds) |
| `dump_icons.py` | Item sprite PNGs | Match `Sprite.PathID` to item icon ref within the **same asset file** (avoids PathID collisions) |
| `dump_coins.py` | Coin sprites | Sprite `m_Name` lookup in `resources.assets` |
| `dump_hotspots.py` | Placed objects across scenes (trees, bushes, fishing, vendors) | Walk every `level*` scene, read Transform world position |
| `dump_maps.py` | Per-scene background PNGs | Composite `Tilemap` layer data |
| `synthesize.py` | Joins raw mono dumps into `data/items.json`, `*.csv` | Pure Python aggregation |

### The TARGETS wall-of-names trick
`dump_mono.py` has a `TARGETS` set of class names you extract. **The single
most common bug:** a ScriptableObject subclass you forgot to add to `TARGETS`
gets silently dropped, and later a whole tab (fuels, spellbooks, fertilizers,
phase items) is mysteriously empty. When something's missing in the UI, check
this set first.

### The PathID collision gotcha (icons and everything else)
Unity PathIDs are **not globally unique across asset files**. If you key sprites
by PathID alone you'll get wrong/colliding icons. `dump_icons.py` solves this by
**scoping lookups to the same asset file** the item came from. Keep this idea —
scope every ID lookup by source file.

### The manual-bytes gotcha (i18n)
`TypeTreeGenerator` sometimes fails on specific asset types (I2 Localization
went out of bounds). When that happens, **hand-decode the raw MonoBehaviour
bytes** using the known field layout (documented at the top of
`dump_i2l.py`). This is fiddly but reliable — you only do it once per asset type.

---

## 5. Phase 3 — The join layer (catalog.py)

`catalog.py` is the bridge between the save file and the extracted data. The
save references items by their **numeric `Item.id`**, while the Unity dumps are
keyed by **PathID**. You must:

1. Load the mono dumps.
2. Build a lookup from `Item.id` → data (name, prices, seasons…).
3. Resolve references: `seed_path_id → seed_item_id`, `harvest_path_id →
   harvest_item_id`, recipe ingredient groups → concrete item ids.

Model it as `Catalog` with `@lru_cache`-ed lookup methods. This is what the plan
engine queries constantly.

---

## 6. Phase 4 — The "plan" engine (the part that makes it a *planner*)

The catalog + save give you raw data. The *planner* part is where you encode
the game's mechanics to answer "what should I do **now**". Study the game's code
(e.g. via decompile) for each mechanic you want to reason about, then encode the
constants:

**Real-world examples from Travellers Rest** (see
[`plan/engine.py`](../planner/plan/engine.py), [`plan/brewing.py`](../planner/plan/brewing.py)):
- **Trends:** `trendPriceMultiplier = 0.2f` → trending items sell for **+20%**.
  Trends rotate every Monday; the save stores **4 weeks ahead** → you can
  compute "plant by X to hit the trend".
- **Aging:** 24h per rank, 48h for rank 3→4 = 5 days for max rank; price
  multipliers rank 2/3/4 = +10/+20/+30%.
- **Currency:** 1 gold = 100 silver = 10,000 copper.
- **Satisfaction:** multiplied straight into the final sale price.
- **Unique bar items bonus:** `+3 copper per unique item × every drink sold`
  → menu *variety* beats *depth*.

Structure: pure functions that take `(state, catalog, translator)` and return
plain dicts (`plan/engine.py:381` `plan_to_dict`). Keep the math **out of the
web server** so it's testable.

---

## 7. Phase 5 — The web app

### Backend (`server/app.py`)
- **FastAPI** app. One endpoint per tab: `/api/saves`, `/api/state`, `/api/plan`,
  `/api/seeds`, `/api/recipes`, `/api/vendors`, `/api/quests`, `/api/perks`,
  `/api/map`/`hotspots`, `/api/fish`, … *(the exact set mirrors your tabs)*
- **WebSocket + watchdog** (`FileSystemEventHandler`): when a save file changes
  on disk, broadcast a `save_changed` message to all connected clients → the UI
  re-renders. This is the "live" feel, and it's *not* polling — it's event-driven.
- Support optional `?lang=` on every endpoint for localization.

### Frontend
- Keep it a **single static file** (`server/static.py` served bundle) — zero JS
  build step, dead simple to deploy and share. (There's an optional React/Vite
  frontend in `planner/web/`; the single-file version is the default.)
- **Localize everything** at render time by passing the selected language.
- Money rendered with the extracted coin sprites; fuzzy global search across
  all tabs.

### Sharing (nice-to-have)
`python -m planner --share` (LAN) / `--tunnel` (ngrok) means friends just open
a URL — no install on their end, and the shopping cart syncs live over the
WebSocket.

### The in-game bridge, in depth (the other full component)

The bridge is **not** just a bolt-on to the planner — it is its own software
with its own repo, build, deploy, HTTP API, and hard threading constraints.
In this repo it lives separately:
`<sibling-repo>/bepinex-seed-bridge/` (a C# BepInEx plugin, `Plugin.cs`,
shipped as `PlannerBridge.dll`). Treat it as a co-equal half of the product.

**What it is:** A BepInEx plugin loaded *inside the running game*. It starts its
own local HTTP listener on a fixed port (`8766`) and exposes the game's live
state and actions to the planner, which runs on the same machine (`8765`).

**Build & deploy (C#/.NET side):**
- `netstandard2.1` project, assembly name `PlannerBridge` (`BepInExSeedBridge.csproj`).
- References the game's own DLLs directly for an **offline build**:
  `BepInEx.dll`, `0Harmony.dll`, `UnityEngine*.dll`, `Assembly-CSharp.dll` —
  all `HintPath`'d from the local install (no NuGet).
- Build: `dotnet build -c Release` → `bin/Release/netstandard2.1/PlannerBridge.dll`.
- Deploy: copy that DLL into `<Game>_Data`'s sibling `.../BepInEx/plugins/`
  (BepInEx loads plugins **at game boot** — copying a DLL while the game runs
  has no effect; you must restart the game to deploy a new bridge build).

**Its HTTP API (planner ↔ bridge contract):**
- **Health/status:** `GET /ping`, `GET /status` (alias `/bridge/status`).
- **Live reads:** `GET /bridge/inventory` (alias `/debug/inventory`),
  `/bridge/state`, `/bridge/events`, `/bridge/methods`.
- **Actions (write):** `POST /addItem` (alias `/addSeed`,
  `{itemId,count}` — also `item_id`/`seedId`, `amount`/`qty`, or query params),
  `POST /money`, `POST /shop/buy`, `POST /shop/sell`.
- **Debug:** `GET /debug/toggle`, `GET /debug/clear`.
- It also keeps an in-memory ring buffer of `BridgeEvent`s and fires
  `NotifyPlannerApp(event)` which `POST`s to the planner's
  `http://127.0.0.1:8765/api/bridge/push` — this is the **bridge → planner →
  websocket → UI** push path (`Plugin.cs` `NotifyPlannerApp`).

**The threading trap (the single biggest bridge gotcha):** BepInEx created the
plugin component (`Awake`/`OnEnable` ran, HTTP server started) but Unity **never
advanced it into the play loop** — `Start()`/`Update()`/`FixedUpdate()` never
fired in Travellers Rest. So any design that drains game-API calls on the main
thread via `Update()` or a coroutine **cannot work here**. The fix that did work:
run game method calls **directly on the HTTP worker thread**, serialized with a
`lock` (`SyncOnMainThread` → inline). Verify for your own game whether
`Plugin.Update()` actually ticks before you rely on main-thread dispatching.

**Bridge↔planner division of labor (see also "two live-update mechanisms"):**
the planner polls the bridge for live counts and merges them over the save view;
the bridge pushes events to the planner's `/api/bridge/push` for sub-second UI
feedback; write actions flow planner → bridge → game. The planner ALWAYS treats
the save parse as truth and the bridge as the fast hint / live delta.

### Security (a live save reader exposes real player state — treat it like a dossier)

This tool reads a player's **current game state** (money, inventory, progress)
and can even *write* to it (cheat/buy/sell). That's sensitive, so gate it:
- **Default to localhost only.** Bind to `127.0.0.1` unless the user *explicitly*
  opts into sharing (`--share` binds `0.0.0.0`, `--tunnel` exposes it publicly).
- **Restrict CORS to an explicit allow-list** of origins (localhost + the
  specific LAN/tunnel URL being served) — the repo builds `_allowed_origins`
  from the running host/port (`app.py:198-207`), not a wildcard.
- **Put an auth token in front of share mode.** The repo generates a
  `SHARE_TOKEN` (random, per-run — `app.py:198`) for exactly this; require it
  on shared/tunneled connections so a public ngrok link isn't an open door to
  someone's save file. (Verify it's actually enforced — see the open note below.)
- **Only the read endpoints are harmless to share; gate the write endpoints**
  (`/api/cheat/*`, `/api/shop/*`) so random viewers can't mutate the host's game.

### Testing (don't skip — this is what keeps it alive across game patches)

The extractors and planner touch real binary formats that the game vendor
changes unpredictably. Lock behavior down with tests that **mock the actual
parsed data** so they run without the game or a save:
- Feed small fixture/stub root objects into `extract()`/`GameState` and assert
  the fields you care about — this is how a future rename is caught as a test
  failure instead of a silent `None`.
- Unit-test the pure plan/brewing math (trends, aging, margins) with
  constructed `Catalog`/`GameState` inputs, not live data.
- For the bridge, mark end-to-end tests that need the game running and **skip
  them automatically when the bridge/save is absent** (see
  `tests/test_live_seed_happy_path.py`).

### The two live-update mechanisms — and why you want BOTH

There are **two independent ways** to keep the UI current. Do not pick one —
they solve different problems and work best *together*:

| | **Watchdog (save-driven)** | **In-game bridge (live API)** |
|---|---|---|
| What it watches | The save file on disk via `watchdog`'s `Observer` | The running game process via an in-game HTTP plugin (Travellers Rest: BepInEx `PlannerBridge` on port 8766) |
| Trigger | The game **autosaves** (file `modified`/`created`/`moved`) | Any in-game event the plugin decides to push (item added, money changed, buy/sell) |
| Latency | When the game writes a save (often seconds apart) | `<200 ms` — near-instant |
| Data | **Authoritative, full `GameState`** (everything: money, date, trends, crops, quests, inventory) — re-parsed from the whole save | **Small deltas / live counts** — e.g. just the current inventory counts for player 1, or "item X granted" |
| Cost | Cheap (one parse per save) | Requires a game mod (BepInEx + Harmony) and a **stable** plugin |
| Failure mode | Works even with zero mods; just has to wait for an autosave | If the plugin/bridge goes offline, it silently degrades |

**In this repo the two are wired together** (see `planner/server/app.py`):

1. **Authoritative refresh = watchdog.** `SaveWatcher(FileSystemEventHandler)`
   watches `File_1`'s save folder, debounces to ~0.2 s, and broadcasts
   `save_changed` over the shared `/ws` WebSocket (`app.py:83-192`). Clients
   re-run `/api/plan` and get the full, save-accurate picture.
2. **Low-latency live reads = bridge.** The planner *polls* the bridge for live
   inventory/money and **merges** those counts into the read so buy/sell shows up
   *instantly* as graphical feedback, before the autosave even lands on disk
   (`_fetch_live_bridge_counts` → merged at `app.py:371`). If the bridge is
   down, it falls back to the save file.
3. **Writes + events = bridge.** Money/seed/shop modifications go to the bridge
   for realtime effect (`/api/cheat/*`, `/api/shop/buy|sell`); the bridge can
   `POST /api/bridge/push`, which the planner rebroadcasts as `bridge_event`
   over `/ws` for <200 ms UI feedback (`app.py:80-81, 1275-1289`).

#### The design rule: bridge = *fast hint*, save = *truth*

```
         autosave ───────────▶ watchdog ──▶ /ws "save_changed" ──▶ re-parse save ──▶ authoritative UI
                                    ▲
   in-game action ──▶ bridge ──▶ /api/bridge/push ──▶ /ws "bridge_event" ──▶ optimistic flash (<200ms)
                                    │
                                    └──▶ planner polls /inventory,/money ──▶ merge live counts
                                                                   (fallback to save if bridge offline)
```

Because the bridge can be flaky (see this repo's
[`docs/BRIDGE_FINDINGS_2026-09-01.md`](BRIDGE_FINDINGS_2026-09-01.md) — BepInEx
in this game yielded an unstable bridge), **never make the bridge the source of
truth.** The save parse is always the canonical, durable state. Use the bridge
for: (a) optimistic UI feedback that the watchdog later confirms/overrides, and
(b) write actions you can't express by editing a save cleanly.

**When you replicate for a new game:**
- **Always build the watchdog path first.** It needs no mod, is robust, and is
  the minimum viable "live" experience.
- **Treat the bridge as an optional accelerator**, only if the game accepts a
  BepInEx-style plugin (i.e. `.NET`/Mono, not IL2CPP, and a stable lifecycle —
  verify `Plugin.Update()` actually fires; in Travellers Rest it never did, so
  the bridge had to call game APIs off-thread).
- **Make every bridge path degrade gracefully** to the save-file path so both
  share the same read. Both should converge on the same `GameState`.
- Expose bridge status so the UI can show "live" vs "offline (save-only)".
- Consume bridge *push* events over the same WebSocket channel you already use
  for `save_changed`, so clients have one socket, two message types.

### Surfacing "is this from live or from the save?" in the UI

Because a value can come from either the bridge (near-instant) or the save parse
(after an autosave), the UI needs a **visible provenance indicator** so a user
isn't misled into thinking unsaved state is permanent. Rule: **label the data
source on every number that differs between the two.**

The backend should already be emitting the raw signal — surface it:
- `/api/inventory/grouped` already returns per-item `{"live": bool, "changed":
  bool, "count": <live>, "save_count": <from-save>}` plus top-level
  `slot_live` / `live_available` when it merges bridge counts
  (`app.py:371-386, 419`). Expose the same on `/api/state` and `/api/plan`.
- Give the UI a **live/save source badge** (a colored pill like the existing
  `#ws-pill` connection dot at `static.py:3759`): e.g. a green *● live* vs an
  amber *● saved (as of HH:MM)* next to the save date. When a specific number
  is provisional, append a small ⚠ "not saved yet" marker and show `save_count`
  as the tooltip.
- Send a **`live_status`** message over `/ws` whenever the bridge drops/reconnects
  so the badge flips without a full reload.

### The "things completed between saves" tracker

This is the feature that most justifies a live bridge at all. **Motivation
(applies to any game that saves infrequently):** if a game only persists on
specific checkpoints (TR saves when you **sleep**, at end of day), then quitting
mid-way can lose a whole play session of progress. But you *know* what you did
in that session (via the bridge), so the tool can track **the delta between save
checkpoints** — turning a data-loss risk into a useful status/undo view.
*(For TR specifically: see the Save cadence row in Appendix A.)*

Design:
1. **The save is the checkpoint.** On each `save_changed`, snapshot the parsed
   `GameState` (money, item counts, crops, quest progress, recipes unlocked)
   and store it as `last_saved = <baseline>`.
2. **The bridge is the live delta.** Each `bridge_event` (item added,
   money changed, buy/sell, quest tick, crop harvested) is appended to a
   monotonic in-memory event log with a timestamp: `{type, item_id, qty,
   delta_copper, at}`.
3. **Compute "since last save"** = current live state *minus* `last_saved`,
   rolled up into human lines: *"+12 Wheat, +5,000c, sold 20 Beer, harvested
   3 Blueberry, quest 'X' ticked"*.
4. **Show it as a banner/drawer**: "Since your last save you have: …" with an
   explicit warning that these are *not* saved — close the game wrong and they're
   gone. That both protects the user and makes the bridge feel purposeful.
5. **Clear the log on `save_changed`** (the snapshot just absorbed it all).
   **Persistence caveat:** keep it in-process by default (no need to persist a
   session log to disk), but be aware that restarting the *planner* mid-day
   loses the one "live" baseline that wasn't yet saved — either accept that, or
   persist `last_saved` + the event log to a small local JSON so a planner
   restart reconstructs the between-saves view.

Concretely, the flow is: watchdog/bridge events both land on the **one** `/ws`;
the client (or a small `/api/session/since-last-save` endpoint) diffes the
current live view against the last-save snapshot and renders the delta. The
bridged `live`/`changed` fields already give you the per-item diff — the 
between-saves log is just those diffs histogrammed against the last snapshot.

---

## 8. The full pipeline checklist (do this for the new game)

- [ ] Locate **save folder**; add override env vars. *(saves.py:55 `saves_root()`)*
- [ ] Hex-inspect a save → **identify serializer**. *(BinaryFormatter → pypdn;
      Sirenix → odin-style walker)*
- [ ] Decompile `Assembly-CSharp.dll`; map the **root save class fields**.
- [ ] Write **`GameState`** dataclass + `extract()` (all fields try/except).
- [ ] Locate **game data folder**; make `gamepath.py` auto-detect it.
- [ ] Extract **ScriptableObjects** (items/recipes/crops/…) → `dumps/mono/`.
- [ ] Extract **localization** terms. *(hand-decode if typetree breaks)*
- [ ] Extract **icons** (PathID-scoped per asset file). *(optional but great)*
- [ ] Extract **hotspots/maps** if the game has a world to visualize.
- [ ] `synthesize.py` → join mono dumps into `data/*.csv|json`.
- [ ] `catalog.py` → bridge save-IDs ↔ extracted data (resolve PathID↔item.id).
- [ ] Encode the **mechanics** that make it a planner (trends, aging, margin…).
- [ ] Build **FastAPI endpoints** + **watchdog WebSocket** re-render.
- [ ] *(Co-equal, optional — own repo/build)* **The in-game bridge**:
      - [ ] Decide it's possible: game is `.NET`/Mono (not IL2CPP) and has a
            stable moddable lifecycle (BepInEx-style).
      - [ ] Write the C# BepInEx plugin: `HttpListener` on a fixed port, event
            ring buffer, `NotifyPlannerApp` → planner `/api/bridge/push`.
      - [ ] Build offline (`dotnet build -c Release`), deploy DLL to
            `<Game>_Data`/..`/BepInEx/plugins/` **and restart the game**.
      - [ ] Wire planner to poll it (`/api/inventory`, `/money`) & merge as a
            *live hint*; always fall back to the save parse as truth.
      - [ ] Verify `Plugin.Update()` actually ticks for your game before relying
            on main-thread dispatch (TR: it didn't → call game APIs off-thread
            under a lock). *(see §7 "the in-game bridge, in depth")*
- [ ] **Source badge** — mark every number with live-vs-save provenance
      (`live`/`changed`/`slot_live` already emitted; surface them in the UI).
- [ ] **"Since last save" tracker** — snapshot `GameState` on `save_changed`;
      log bridge deltas in between; diff & render "things completed since your
      last save". *(TR saves only on sleep, so this is high-value)*
- [ ] Build the **UI** (start single-file static; add tabs one at a time).
- [ ] **Tests** for: catalog, currency, save parser, brewing/trend math, API,
      and bridge (auto-skip e2e when the game/bridge is absent).
- [ ] `.gitignore` the extracted assets (`data/`, `dumps/`) — **never commit
      copyrighted game content**.

---

## 9. Gotchas that burned the most time (read these twice)

1. **Serializer was wrong.** ASSUMED Odin, turned out BinaryFormatter. Check the
   actual bytes first; don't trust the game's marketing/dependency rumors.
2. **Obfuscator mangles method names, not fields.** The game is Beebyte-obfuscated
   (method names like `GNJGDCKAOEF`), but data extraction works against **field
   names**, which survive. Extract by field name — never call game methods.
3. **PathID collisions across asset files.** Scope all sprite/object lookups to
   the source asset file or you get silently-wrong data.
4. **`TARGETS` set omissions silently drop data.** If a tab is empty, your
   extractor isn't listing that ScriptableObject class.
5. **typetree out-of-bounds on some assets** (I2 Localization). Hand-decode the
   bytes with a documented field layout instead of fighting the typetree.
6. **Fragile parsers.** A single changed/renamed field (game update) used to
   crash the whole read. Every field access is now try/except with a default —
   **this is non-negotiable** in any save parser.
7. **Don't key by PathID in the save → don't call live runtime methods either.**
   The save-file field names (`itemsInInventory`, etc.) exist only in the save;
   the live runtime `PlayerInventory` MonoBehaviour has *different* field names.
   (See [`docs/BRIDGE_FINDINGS_2026-09-01.md`](BRIDGE_FINDINGS_2026-09-01.md)
   for the full saga of trying to bridge into the live game — a separate,
   optional adventure involving BepInEx + Harmony. Not required for the
   planner itself.)
8. **Game data is copyright.** Keep `data/` and `dumps/` in `.gitignore`; ship
   only extractor *scripts*. Users run them against their own legally-owned
   game copy.

---

## 10. File-by-file map (as a starting skeleton)

```
<new-game>-planner/
├── planner/
│   ├── __main__.py          # `python -m planner [--share|--tunnel]`
│   ├── gamepath.py          # auto-detect game data folder (env override)
│   ├── catalog.py           # save-id ↔ extracted-data bridge (the "wiki")
│   ├── i18n.py              # localization lookup (Translator class)
│   ├── parser/
│   │   ├── saves.py         # discover + parse + extract -> GameState
│   │   └── odin.py          # (if game uses Sirenix Odin format)
│   ├── plan/                # pure computation, testable, no I/O
│   │   ├── engine.py        # main "what do I do now" plan
│   │   ├── brewing.py       # multi-stage craft chain walker + aging math
│   │   ├── recipes.py, seeds.py, brew_planner.py, itemdb.py
│   └── server/
│       ├── app.py           # FastAPI + WebSocket watchdog + all /api/*
│       └── static.py        # single-file UI (or web/ for React)
├── scripts/                 # one-shot extractors (idempotent)
│   ├── dump_mono.py / dump_i2l.py / dump_icons.py / dump_coins.py
│   ├── dump_hotspots.py / dump_maps.py / synthesize.py
├── data/                    # extractor output (gitignored)
├── dumps/                   # raw extractor output (gitignored)
├── tests/                   # pytest
├── install.py               # one-click setup: deps -> find game -> extract -> verify
├── requirements.txt
└── README.md

# Optional co-equal sibling repo — the in-game bridge (C#/BepInEx plugin)
<new-game>-bridge/
├── Plugin.cs                # BepInEx BaseUnityPlugin: HttpListener + event log
│                            #   + NotifyPlannerApp (POST -> planner /api/bridge/push)
├── <Bridge>.csproj          # netstandard2.1; HintPath refs to game/BepInEx DLLs (offline)
└── README.md                # endpoints, build (`dotnet build -c Release`), deploy steps
```

---

## 11. Suggested adaptation order (the 80/20)

If you only have a weekend, do it in this order and you'll have something useful:

1. **Save reader + GameState** (hardest, biggest payoff — you get the "live
   state" half).
2. **One good extractor** that gets the core item/recipe table so data has
   names. (Skip icons/maps/i18n initially.)
3. **FastAPI + plan engine** for the single most valuable question (e.g. for a
   farming game: "what should I plant/sell now for best profit/timing").
4. **Watchdog WebSocket live-refresh** (cheap, and it's the "wow" feature).
5. ***(Only if the game has a stable moddable runtime)* the in-game bridge** —
   add it as a *fast-hint overlay* on the existing watchdog WebSocket, never as
   the source of truth. It's the cherry on top, not a prerequisite.
6. **Everything else is garnish**: icons, maps, localizations, extra tabs, and
   finally the one-click installer.

The single-file-static-UI + localhost-only default keeps you shipping fast;
add sharing and the installer only after the fundamentals work.

---

## Appendix A. Worked example profile — Travellers Rest

The rest of this document is the *general* method for "any game + save file."
This appendix pins down the concrete facts for the reference game, so when you
pick a new game you can fill in the same table and every general step becomes
concrete. Treat every row as "what to discover/verify for YOUR game."

| Attribute | Travellers Rest value |
|---|---|
| **Genre** | Cozy tavern-management / life-sim farming sim (single-player, optional co-op) |
| **Engine** | Unity |
| **Engine version** | `2022.3.62.7762112` (Unity 2022.3.x) |
| **Runtime model** | **Mono** (not IL2CPP) — `<Game>_Data/Managed/` exists and `Assembly-CSharp.dll` is managed IL |
| **Asset format** | Unity bundles: `*.assets`, `sharedassets*`, `level0`–`levelN`, `globalgamemanagers`, `resources.assets` |
| **Data extractor** | `UnityPy` + `TypeTreeGenerator`; `UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0"` |
| **Save location** | `%USERPROFILE%\AppData\LocalLow\Louqou\TravellersRest\GameSaves\File_1\SaveFile*.save` (`LocalLow\<Company>\<Game>`) |
| **Save format** | .NET **BinaryFormatter** (NRBF) — *not* Sirenix Odin (the repo keeps both readers) |
| **Save parser** | `pypdn` (`NRBF` → `resolveReferences` → `getRoot`), monkey-patched for newer `Dictionary<int,…>` fields |
| **Save cadence** | **Only on sleep / end of day** — mid-day state is unsaved → the "since last save" tracker matters |
| **Obfuscation** | Beebyte Obfuscator 3.12.0 — method names mangled (e.g. `GNJGDCKAOEF`), **field names survive** |
| **Live bridge** | BepInEx 5.4.23.4 Mono plugin (`PlannerBridge`), HTTP port 8766; bridge `Plugin.Update()` never fired → needed off-thread API calls |
| **Bridge repo** | Separate sibling repo `bepinex-seed-bridge/` (C# `netstandard2.1`, `Plugin.cs`, shipped `PlannerBridge.dll`); bridge↔planner contract: planner polls `:8766`, bridge `POST`s events back to planner `:8765/api/bridge/push` |
| **Key mechanics encoded** | Trends +20%; aging 24/24/24/48 h = 5 days max, rank price +10/+20/+30%; 1g = 100s = 10,000c; satisfaction × into sale price; +3c / unique bar item |

**Why this appendix exists:** it separates *"how to mine any game"* (the general
method) from *"what Travellers Rest looked like"* (the reference instance). When
replicating, fill in this exact table for the new game — the moment you know the
**Save format** and **Engine / runtime model** rows you've already made the
biggest architectural decisions (which parser, and whether a live bridge is even
possible). Everything else in the body follows mechanically.

