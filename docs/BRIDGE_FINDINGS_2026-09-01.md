# PlannerBridge live seed add — diagnostic findings
**Date:** 2026-09-01 · **Author:** opencode session · **Status:** IN PROGRESS (item grant + read-back not yet green; root cause of main-thread dispatch identified and confirmed)

## Objective
Make the Travellers Rest live bridge (`bepinex-seed-bridge`) support the happy path:
POST an "add seed" request → BepInEx bridge `/addItem` → game actually grants the item → live read-back via the bridge shows the added count.

Reference test: `tests/test_live_seed_happy_path.py` (2 tests, both currently fail/then-skip).

---

## WHAT WORKS (verified live)

### `/debug/inventory` — FIXED & VERIFIED
The live inventory read now works correctly after switching to the game's own API:

- `PlayerInventory.GetPlayer(1)` → authoritative player-1 inventory (static field `GNJGDCKAOEF`).
- `player.GetAllItems()` → `Dictionary<int, ItemAmount>` (itemId → amount), merges main inventory + action bar.
- Verified response: **30–42 real items with correct stacks** (e.g. `{"itemId":1040,"stack":50}`), plus `copper` populated (1223257 → 1242300). No more "no inventory found", no more empty list.

**Reflection caveat discovered:** `GetPlayer` is `GetPlayer(int, bool, bool)` (obfuscated name `ICPOKIFPNLG` in some copies). Passing only `1` throws *"Number of parameters specified does not match the expected number."* **Pass `new object[]{1, false, false}`.** Also, plain `FindObjectOfType<PlayerInventory>()` may return the wrong (ghost/networked) instance yielding an empty inventory — must use `GetPlayer(1)`.

### Save parser (planner, not bridge)
`planner/parser/saves.py` (lines 247–263, 396–406) reads serialized fields `itemsInInventory`, `itemsInInventory2`, `itemsInBuildingInventory`, `orderBox`, `itemsInActionBar`, `itemsInActionBar2` — each a `SlotSave[]` with `itemInstanceSave.itemID` + `stack`. **These are SAVE-file field names only; they do NOT exist on the live runtime `PlayerInventory` MonoBehaviour.** (This is why the bridge's earlier field-scan returned empty.)

---

## ROOT CAUSE OF THE `/addItem` TIMEOUT (CONFIRMED)

### Symptom
`POST /addItem` always returns `{"ok":true,...,"result":"timeout"}` (elapsed ~2500 ms). The bridge log shows the final `AddItem 3099 x12 syncResult=timeout (elapsed 2502ms)` line but **never the first line of `AddItem()` (`"AddItem 3099 x12 requested [verbose]"`)** — the main-thread callback never executes within the 2.5 s window.

### The dispatcher can never drain — because the plugin never enters Unity's play loop
The bridge dispatched game API calls to the main thread via `MainThreadDispatcher`, drained from `Plugin.Update()`. **Diagnostic proof (single diagnostic build) that Unity does NOT drive plugin MonoBehaviours:**

| Lifecycle method | Fired? | Evidence |
|---|---|---|
| `OnEnable()` | ✅ YES | `Plugin.OnEnable() fires` logged |
| `Awake()` | ✅ YES | HTTP server starts, bridges respond |
| `Start()` | ❌ NO | never logged |
| `Update()` | ❌ NO | `Plugin.Update tick #1` never logged (0 over a long session) |
| `FixedUpdate()` | ❌ NO | never logged |

So BepInEx creates/enables the plugin component (`OnEnable`/`Awake` run) but Unity **never advances it into the active play loop** (`Start`/`Update`/`FixedUpdate` never fire). Without a ticking MonoBehaviour, a queue can never be drained on the main thread. This is abnormal for BepInEx Mono (5.4.23.4), but the evidence is unambiguous across repeated restarts.

### Why the standalone dispatcher also fails
`MainThreadDispatcher` is created by a `[RuntimeInitializeOnLoadMethod(BeforeSceneLoad)]` MonoBehaviour. That attribute runs during the game's first scene load, **before BepInEx loads plugins**, so the type isn't loaded yet and the runtimes attribute never fires in practice — and even if created, a standalone MonoBehaviour also has the non-ticking-update problem above.

### BepInEx environment (confirmed)
- BepInEx **5.4.23.4** Mono (winhttp.dll Doorstop, `TravellersRest_Data/Managed/` exists → Mono, not IL2CPP).
- Unity **2022.3.62.7762112**.
- `0Harmony.dll`, `BepInEx.Harmony.dll`, `MonoMod.RuntimeDetour.dll`, `MonoMod.Utils.dll` all present in `BepInEx/core/`.

### Implications
Any fix relying on `Plugin.Update()`, `MainThreadDispatcher`, or coroutines (coroutines also need Unity to pump the component) **cannot work in this game**. Options are:
1. **Call the game API directly on the HTTP worker thread** (serialized with a `lock`). Unity/Mono tolerates off-thread calls to pure-managed game methods; `AddItems` + `GetAllItems` are pure managed. This is the fastest empirical test and the currently-deployed approach — **not yet proven green** (tests were skipped because the bridge went offline before the run).
2. **Hook a per-frame game method** via Harmony or MonoMod.RuntimeDetour and drain the queue in a postfix/after-hook. Robust but requires locating an obfuscated per-frame `Update` on an always-active game MonoBehaviour; not yet attempted.

---

## CURRENT BRIDGE CODE STATE
Source: `/home/cmayfield/code/games/bepinex-seed-bridge/Plugin.cs` (repo has a *corrupt single commit* `f3dfedf` — Plugin.cs must be repaired, `bin/obj` untracked).

Uncommitted changes on top of the (repaired) version that is byte-equivalent to the deployed v1.1.0 DLL:

1. **`SyncOnMainThread` → direct inline execution** (currently deployed `e3f2bd9b…`):
   ```csharp
   private static readonly object _apiLock = new object();
   private string SyncOnMainThread(Func<string> fn, int timeoutMs = 2500)
   {
       lock (_apiLock)
       {
           try { return fn(); }
           catch (Exception ex) { _log.LogError($"inline game call: {ex}"); return ex.Message; }
       }
   }
   ```
   No more `ManualResetEvent`/`MainThreadDispatcher.Enqueue`. Runs `fn()` on the HTTP worker thread immediately.
2. **`BuildInventoryJson()`** uses `PlayerInventory.GetPlayer(1).GetAllItems()` → `Dictionary<int,ItemAmount>`; reads each value's `amount` field. (Deployed and verified working.)
3. **`AddItem()`** also prefers `GetPlayer(1,false,false)` for the target inventory (fallback to `FindObjectOfType`), then calls in order: `AddItems(ItemInstance,int)` → `AddItem(ItemInstance)` ×count → `AddItem(int)` ×count → `AddItemInstance(int,ItemInstance)`.
4. **Lifecycle diagnostics** temporarily in `Plugin` (`Start`/`OnEnable`/`Update tick`/`FixedUpdate`) — **remove these before finalizing; they have served their purpose.**
5. `MainThreadDispatcher` left in place (non-functional, harmless), `TryForceSaveAfterAdd` still enqueues to it (harmless no-op now).

Review the Diff to see 5 changed files.
Deployed DLL (currently in `.../Windows/BepInEx/plugins/PlannerBridge/PlannerBridge.dll`): sha256 `e3f2bd9b21481c5ce0b437f7166b7e91e53bf198e8f440252dcd6455c44062b6` (the inline-off-thread build). Prior verified-good backstop: `/tmp/opencode/PlannerBridge.deployed.v1.1.0.dll`.

---

## KEY GAME SCHEMA (from `ilspycmd` decompile of `Assembly-CSharp.dll`)
- `public class PlayerInventory : MonoBehaviour` — has `public Inventory inventory`, `public ActionBarInventory actionBarInventory`, `Slot[] slots`, static `GNJGDCKAOEF` / `IKPJHDOBNIF` / `HIBHGNMIDMF[5]`.
  - `public Dictionary<int, ItemAmount> GetAllItems()` (line 1198) — **authoritative read** (merges inventory + actionBarInventory).
  - `public static PlayerInventory GetPlayer(int, bool = false, bool = false)` (line 1114) → returns `GNJGDCKAOEF` for player 1.
  - `public int AddItems(ItemInstance, int, bool=false, bool=true, bool=true, bool=false)` (line 267).
  - `public bool AddItem(ItemInstance, bool=false, bool=true, bool=true, bool=true)` (line 725).
  - `public bool AddItem(int, bool=false, bool=true, bool=true, bool=true)` (line 827).
  - `public int NumberOfItems(int)` — per-item count.
- `public class Inventory : Container` — `slots`, `GetAllItems()`, static player getters.
- `Money.ToCopper()` (static) — provides the `copper` value.
- Most method names are **obfuscated** (random alphanumeric names) except the inventory API names above, which are clean.

---

## TEST HARNESS BEHAVIOR
`tests/test_live_seed_happy_path.py`:
- Skips the whole module if the **planner** `/api/bridge/status` says bridge offline. (Note: `/api/bridge/status` on the planner reports `{"bridge":false,...}` because the bridge direct call `/bridge/status` on 8766 timed out/returned empty at that moment — **the bridge is unstable, going intermittently offline**, matching the user's "bridge is offline" report. The bridge must be stayed-up for the test run.)
- `test_happy_path_add_random_seed_live`: RNG seed 12 → picks seed item **3099**, count **12**; POSTs via planner `/api/cheat/seed`; polls live read-back until count = baseline + 12.
- `test_bridge_reports_add_item_without_timeout`: direct `POST /addItem` (planner bypass), asserts no "timeout".
- **Note:** `POST /addItem` needs a **JSON body** (`{"itemId":..,"count":..}`), not a query string; a body-less `POST -X` returns HTTP 411 "Length Required".
- Run via: `cd …/travellers-rest-planner && source .venv/bin/activate && pytest tests/test_live_seed_happy_path.py -v -s`

---

## TOOLING RECAP
- `ilspycmd` 8.2.0.7535 at `/tmp/opencode/tools/ilspycmd` (needs `export PATH="$HOME/.dotnet:$PATH" DOTNET_ROOT="$HOME/.dotnet"`; .NET 8 SDK 8.0.424 + runtimes 6.0.36/8.0.30 in `~/.dotnet`). Decompiled game in `/tmp/opencode/types/`; deployed bridge in `/tmp/opencode/decompiled/`.
- Bridge build: `cd …/bepinex-seed-bridge && dotnet build -c Release` → `bin/Release/netstandard2.1/PlannerBridge.dll`.
- Deploy: `cp …/PlannerBridge.dll …/Travellers Rest/Windows/BepInEx/plugins/PlannerBridge/`. **Requires stopping the game first** (BepInEx loads plugin DLLs at boot).
- Restart game: `cd ~/.local/share/Steam && ./steam steam://run/1139980`. Bridge port **8766**. Planner port **8765** (PID 497166).

---

## OPEN ITEMS / NEXT STEPS
1. **Prove the inline off-thread `/addItem` either grants the item or crashes.** Keep the game up (bridge stable) and run the happy-path test. If it works → unblocked. If it crashes/doesn't grant → **Unity requires main-thread access → implement a Harmony/MonoMod postfix drain on a per-frame game method** (option 2).
2. **Remove the temporary lifecycle diagnostics** (`Start`/`OnEnable`/`Update tick`/`FixedUpdate` logs) from `Plugin.cs`.
3. **Stabilize the bridge** so `/api/bridge/status` (planner) sees it online during the test — investigate the intermittent 8766 timeouts.
4. Consider committing the cumulative `Plugin.cs` fixes to the bridge repo only when the user explicitly requests it. The repo currently has a corrupt baseline commit; do not commit until asked.