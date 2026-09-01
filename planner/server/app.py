"""FastAPI backend for the Travellers Rest planner.

Endpoints:
  GET  /api/saves                 list available save slots
  GET  /api/state?slot=&lang=     return current GameState (raw)
  GET  /api/plan?slot=&lang=      return computed Plan (the main payload)
  GET  /api/languages             list available locales
  WS   /ws                        push 'updated' messages when a watched
                                  save file changes on disk

Static UI is served from planner/web/dist/ at /. In dev you can run the
React+Vite frontend separately on :5173 and it will hit /api/* via proxy.
"""
from __future__ import annotations

import asyncio
import json as _json_mod
import json
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from planner.catalog import load_catalog
from planner.i18n import available_languages, DEFAULT_LANG
from planner.parser.saves import (
    discover_slots, get_slot, parse_save, extract, saves_root, latest_save_in_folder,
)
from planner.plan.engine import build_plan, plan_to_dict
from planner.plan.brewing import all_brew_plans, build_brew_plan
from planner.plan.brew_planner import build_brew_plan_view
from planner.plan.seeds import build_seed_table
from planner.plan.recipes import list_craftables, get_recipe_detail
from planner.plan.itemdb import item_detail
from planner.i18n import Translator
from dataclasses import asdict, is_dataclass


# ---------- WebSocket connection manager ------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []
        self.lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self.lock:
            self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict):
        text = json.dumps(msg)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------- Watchdog observer — SINGLE FILE REALTIME -------------------------
# Realtime: only File_1/* .save is watched. Debounce 200ms (faster than old 500ms).
# Every emit logs size/mtime/debug and includes slot_id so the web UI can pulse
# with graphical feedback without polling all slots.
# Bridge events (from PlannerBridge) also come in via /api/bridge/push and are
# rebroadcast as bridge_event for even faster UI feedback (<200ms).

class SaveWatcher(FileSystemEventHandler):
    """Watches ONLY File_1's latest .save (single-file realtime mode)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._last_emit_at = 0.0
        self._last_size = 0
        self._last_mtime = 0.0

    def _emit(self, path: str, reason: str = "watchdog"):
        import time, os as _os
        now = time.time()
        # Faster debounce for single-file mode: 0.2s vs old 0.5s
        if now - self._last_emit_at < 0.2:
            return
        self._last_emit_at = now
        # Debug: stat the file
        try:
            st = _os.stat(path)
            size = st.st_size
            mtime = st.st_mtime
            print(f"[planner watcher] {reason} {path} size={size} mtime={mtime:.3f} dt={now-mtime:.2f}s", flush=True)
            self._last_size = size
            self._last_mtime = mtime
        except Exception as e:
            print(f"[planner watcher] stat failed {path}: {e}", flush=True)
            size = 0
            mtime = now
        # Single-file guard: ignore anything not in File_1
        if "File_1" not in path and "SaveAnywhere" in path:
            print(f"[planner watcher] ignoring non-single-file {path}", flush=True)
            return
        coro = manager.broadcast({
            "type": "save_changed",
            "path": path,
            "slot": "File_1",
            "size": size,
            "mtime": mtime,
            "reason": reason,
            "single_file": True,
        })
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except Exception as e:
            print(f"[planner watcher] broadcast failed: {e}", flush=True)

    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".save"):
            # only File_1 in realtime mode
            if "File_1" in event.src_path:
                self._emit(event.src_path, reason="modified")
            else:
                # debug log but don't emit
                import time
                print(f"[planner watcher] skip non-File_1 modified {event.src_path}", flush=True)

    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".save") and "File_1" in event.src_path:
            self._emit(event.src_path, reason="created")

    def on_moved(self, event):
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "") or ""
        if dest.endswith(".save") and "File_1" in dest:
            self._emit(dest, reason="moved")


_observer: Observer | None = None
_single_file_mode = os.environ.get("TR_SINGLE_FILE", "1") != "0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _observer
    loop = asyncio.get_running_loop()
    root = saves_root()
    slot = get_slot("File_1")
    watch_path = None
    # Single-file: watch only File_1 folder, not whole GameSaves root
    if _single_file_mode and slot and os.path.isdir(slot.folder):
        watch_path = slot.folder
        print(f"[planner] SINGLE-FILE REALTIME mode: watching ONLY {watch_path} (File_1) — SaveAnywhere disabled", flush=True)
        try:
            latest = latest_save_in_folder(slot.folder)
            if latest and os.path.isfile(latest):
                import time as _t
                st = os.stat(latest)
                print(f"[planner] single file {latest} size={st.st_size} mtime={st.st_mtime:.1f} ({_t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime(st.st_mtime))})", flush=True)
        except Exception as e:
            print(f"[planner] stat single file failed: {e}", flush=True)
    elif os.path.isdir(root):
        watch_path = root
        print(f"[planner] watching {root} (recursive={not _single_file_mode})", flush=True)
    if watch_path and os.path.isdir(watch_path):
        _observer = Observer()
        # single-file: non-recursive watch on File_1 folder; legacy: recursive on root
        _observer.schedule(SaveWatcher(loop), watch_path, recursive=not _single_file_mode)
        _observer.start()
        print(f"[planner] observer started single_file={_single_file_mode}", flush=True)
    # Bridge heartbeat -> live_status watcher (G0/G1): flips the UI mode badge
    # within ~1s of the heartbeat arriving or timing out.
    live_task = asyncio.create_task(_live_status_watcher())
    yield
    live_task.cancel()
    if _observer:
        _observer.stop()
        _observer.join()
        print("[planner] observer stopped", flush=True)


app = FastAPI(title="Travellers Rest Planner", lifespan=lifespan)

# CORS — restrict when sharing, open for localhost dev
import secrets as _secrets
SHARE_TOKEN = _secrets.token_urlsafe(16)
_allowed_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Share-mode write auth (gap SEC-1) ----------------------------------
# Default mode binds 127.0.0.1 — no token needed. In --share/--tunnel mode
# (TR_SHARE=1), game-mutating endpoints require the per-run SHARE_TOKEN so a
# public link can't mutate the host's game. The host's own direct-localhost
# browser stays frictionless; ngrok-proxied traffic (loopback + X-Forwarded-For)
# is NOT treated as local.

def _is_local_client(request: Request) -> bool:
    """True only for direct loopback connections. Tunnel traffic arrives from
    127.0.0.1 too but always carries X-Forwarded-For, so it is not local."""
    if request.headers.get("x-forwarded-for"):
        return False
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _share_mode() -> bool:
    return os.environ.get("TR_SHARE", "0") == "1"


def _write_denied(request: Request):
    """Token gate for game-mutating endpoints. Returns a 401 response when
    share-mode auth fails, else None (allow)."""
    if not _share_mode() or _is_local_client(request):
        return None
    supplied = request.headers.get("x-share-token") or request.query_params.get("token")
    if supplied and _secrets.compare_digest(str(supplied), SHARE_TOKEN):
        return None
    return JSONResponse(
        {"error": "share token required — open the share link with its #t= token (or send the X-Share-Token header)"},
        status_code=401,
    )


# ---------- Bridge heartbeat / live mode (gap G0/G1) ---------------------------
# The bridge heartbeats every ~2s while the game runs. The planner derives
# "live" purely from heartbeat freshness: first beat -> live mode, ~3 missed
# beats -> save-only. The planner is the non-fragile side — no heartbeat just
# means save-only reads; it never needs the bridge to survive.

_bridge_last_beat: float = 0.0
_bridge_last_info: dict = {}
HEARTBEAT_TIMEOUT_S = 6.0  # default; override for tests via TR_HEARTBEAT_TIMEOUT


def _heartbeat_timeout_s() -> float:
    return float(os.environ.get("TR_HEARTBEAT_TIMEOUT", str(HEARTBEAT_TIMEOUT_S)))


def _bridge_bases() -> list[str]:
    """Bridge base URL(s) to try. Override with TR_BRIDGE_BASE (single URL) —
    used by tests to point at the bridge simulator, or to move the bridge to
    another port. Default: loopback, then localhost."""
    env = os.environ.get("TR_BRIDGE_BASE")
    return [env] if env else ["http://127.0.0.1:8766", "http://localhost:8766"]


def _bridge_live() -> bool:
    return (time.time() - _bridge_last_beat) < _heartbeat_timeout_s()


async def _live_status_watcher():
    """Broadcast live_status over /ws whenever live mode flips (G1)."""
    last_live: bool | None = None
    while True:
        await asyncio.sleep(1.0)
        live = _bridge_live()
        if live != last_live:
            last_live = live
            age = (time.time() - _bridge_last_beat) if _bridge_last_beat else None
            try:
                await manager.broadcast({
                    "type": "live_status",
                    "live": live,
                    "heartbeat_age_s": round(age, 1) if age is not None else None,
                })
                print(f"[planner] live_status: {'live (bridge heartbeat fresh)' if live else 'save-only (no bridge heartbeat)'}", flush=True)
            except Exception:
                pass


# ---------- Routes -----------------------------------------------------------

@app.get("/api/saves")
def api_saves():
    slots = discover_slots()
    return [
        {
            "slot_id": s.slot_id,
            "label": s.label,
            "mtime": s.mtime,
            "latest_file": s.latest_file,
        }
        for s in slots
    ]


@app.get("/api/languages")
def api_languages():
    return [{"name": l["name"], "code": l["code"]} for l in available_languages()]


def _load_state_for(slot_id: str | None):
    slot = get_slot(slot_id)
    if not slot:
        return None
    # Re-discover the latest save in the slot folder so we always pick up the freshest
    latest = latest_save_in_folder(slot.folder) or slot.latest_file
    mt = os.path.getmtime(latest)
    try:
        root = parse_save(latest)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise
    try:
        return extract(root, slot_id=slot.slot_id, save_path=latest, save_mtime=mt)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise


@app.get("/api/state")
def api_state(slot: str | None = Query(default=None)):
    state = _load_state_for(slot)
    if state is None:
        return JSONResponse({"error": "no save"}, status_code=404)
    return {
        "slot_id": state.slot_id,
        "save_mtime": state.save_mtime,
        "money_copper": state.money_copper,
        "tavern_rep": state.tavern_rep,
        "days_to_next_trend": state.days_to_next_trend,
        "current_date": vars(state.current_date),
        "trends": [
            {
                "food_ids": t.food_ids,
                "drink_ids": t.drink_ids,
                "ingredient_ids": t.ingredient_ids,
            } for t in state.trends
        ],
        "unlocked_recipe_ids": list(state.unlocked_recipe_ids),
        "planted_crop_counts": state.planted_crop_counts,
        "item_counts": state.item_counts,
    }


def _fetch_live_bridge_counts(timeout: float = 0.8) -> dict[int, int] | None:
    """Try to fetch live inventory from PlannerBridge for realtime graphical feedback.
    Returns {itemId: count} if bridge is live and inventory endpoint succeeds, else None.
    This lets buy/sell appear instantly without waiting for save file autosave.
    """
    import urllib.request, json as _j
    for base in _bridge_bases():
        try:
            with urllib.request.urlopen(base + "/debug/inventory", timeout=timeout) as r:
                j = _j.loads(r.read().decode())
                if j.get("ok") and isinstance(j.get("items"), list):
                    counts: dict[int, int] = {}
                    for it in j["items"]:
                        iid = it.get("itemId") or it.get("item_id")
                        stack = it.get("stack", 1)
                        if iid:
                            counts[int(iid)] = counts.get(int(iid), 0) + int(stack)
                    if counts:
                        return counts
        except Exception:
            continue
    return None


def _fetch_live_bridge_money(timeout: float = 0.6) -> int | None:
    import urllib.request, json as _j
    for base in _bridge_bases():
        try:
            with urllib.request.urlopen(base + "/debug/state", timeout=timeout) as r:
                j = _j.loads(r.read().decode())
                if "copper" in j:
                    return int(j["copper"])
        except Exception:
            continue
    return None


@app.get("/api/inventory/grouped")
def api_inventory_grouped(slot: str | None = Query(default=None), lang: str = Query(default=DEFAULT_LANG)):
    """Inventory grouped by where you get the item (vendor/crop/forage/fish/crafted/other).
    Merges live bridge counts when available so buy/sell is visible instantly (graphical feedback)
    before the game autosaves to disk. Falls back to save file if bridge offline.
    """
    state = _load_state_for(slot)
    if state is None:
        return JSONResponse({"error": "no save"}, status_code=404)
    cat = load_catalog()
    tr = Translator(lang)
    # Build lookup for quick source checks
    crop_by_harvest = cat.crops_by_harvest_item_id
    crop_by_seed = {c.seed_item_id: c for c in cat.crops_by_id.values() if c.seed_item_id}
    fish_ids = {f.fish_id for f in cat.fishes}
    # bush harvests
    bush_harvest_ids = set()
    for b in cat.bushes:
        d = b.raw
        h = d.get("harvestedItems") or d.get("harvestedItem")
        if isinstance(h, dict):
            inner = h.get("item") or h
            pid = inner.get("m_PathID", 0) if isinstance(inner, dict) else 0
            it = cat.items_by_path_id.get(pid)
            if it: bush_harvest_ids.add(it.item_id)
        elif isinstance(h, list):
            for entry in h:
                if isinstance(entry, dict):
                    inner = entry.get("item") or entry
                    pid = inner.get("m_PathID", 0) if isinstance(inner, dict) else 0
                    it = cat.items_by_path_id.get(pid)
                    if it: bush_harvest_ids.add(it.item_id)
    # vendor map: item_id -> [vendors]
    vendor_map: dict[int, list[str]] = {}
    for shop in cat.shops:
        for si in shop.items:
            pid = (si.get("item") or {}).get("m_PathID", 0)
            it = cat.items_by_path_id.get(pid)
            if it:
                vendor_map.setdefault(it.item_id, []).append(shop.name)
    # recipe outputs
    crafted_ids = {r.output_item_id for r in cat.recipes_by_id.values() if r.active and r.output_item_id}

    groups: dict[str, list[dict]] = {
        "Farming (harvest)": [],
        "Seeds": [],
        "Foraging": [],
        "Fishing": [],
        "Vendors": [],
        "Crafted": [],
        "Other": [],
    }
    vendor_sub: dict[str, list[dict]] = {}

    # Live bridge merging for realtime graphical feedback: if bridge is up, use live counts
    # so the UI updates <300ms even before the game flushes the .save to disk.
    live_counts = _fetch_live_bridge_counts()
    # keep save counts for diff highlighting
    save_counts = state.item_counts
    use_counts = live_counts if live_counts is not None else state.item_counts
    is_live = live_counts is not None

    for iid, cnt in use_counts.items():
        item = cat.items_by_id.get(iid)
        name = tr.item(iid, item.name_id if item else None, fallback=item.name if item else f"#{iid}")
        # Determine primary source in priority order
        # Include realtime flag for graphical pulse in web UI
        save_cnt = save_counts.get(iid, 0)
        changed = is_live and save_cnt != cnt
        entry = {"item_id": iid, "name": name, "count": cnt, "save_count": save_cnt, "live": is_live, "changed": changed, "buy_copper": item.buy_copper if item else 0, "sell_copper": item.sell_copper if item else 0}
        if iid in crop_by_harvest:
            crop = crop_by_harvest[iid]
            entry["detail"] = f"harvest {crop.crop_id}: {tr.crop(crop.name_id, crop.harvest_item_id, crop.name)}"
            groups["Farming (harvest)"].append(entry)
        elif iid in crop_by_seed:
            crop = crop_by_seed[iid]
            entry["detail"] = f"seed for {tr.crop(crop.name_id, crop.harvest_item_id, crop.name)}"
            groups["Seeds"].append(entry)
        elif iid in bush_harvest_ids:
            groups["Foraging"].append(entry)
        elif iid in fish_ids:
            groups["Fishing"].append(entry)
        elif iid in vendor_map:
            # also add to vendor subgroup
            vendors = ", ".join(sorted(set(vendor_map[iid]))[:3])
            entry["detail"] = f"sold by {vendors}"
            groups["Vendors"].append(entry)
            for v in set(vendor_map[iid]):
                vendor_sub.setdefault(v, []).append(entry)
        elif iid in crafted_ids:
            groups["Crafted"].append(entry)
        else:
            groups["Other"].append(entry)

    # Sort each group by count desc
    for k in groups:
        groups[k].sort(key=lambda x: -x["count"])
    for k in vendor_sub:
        vendor_sub[k].sort(key=lambda x: -x["count"])

    return {
        "slot_id": state.slot_id,
        "slot_live": is_live,
        "save_mtime": state.save_mtime,
        "groups": groups,
        "vendor_subgroups": {k: v for k, v in sorted(vendor_sub.items())},
        "total_items": len(use_counts),
        "total_count": sum(use_counts.values()),
        "save_total_items": len(save_counts),
        "save_total_count": sum(save_counts.values()),
        "live_available": is_live,
    }


@app.get("/api/plan")
def api_plan(
    slot: str | None = Query(default=None),
    lang: str = Query(default=DEFAULT_LANG),
):
    try:
        state = _load_state_for(slot)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": "save parse failed"}, status_code=500)
    if state is None:
        return JSONResponse({"error": "no save"}, status_code=404)
    try:
        cat = load_catalog()
        plan = build_plan(state, cat, language=lang)
        return plan_to_dict(plan)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": "plan build failed"}, status_code=500)


def _to_jsonable(obj):
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


@app.get("/api/seeds")
def api_seeds(slot: str | None = Query(default=None),
              lang: str = Query(default=DEFAULT_LANG)):
    state = _load_state_for(slot)
    cat = load_catalog()
    return build_seed_table(state, cat, language=lang)


def _vendor_locations() -> dict[str, list[dict]]:
    """Map vendor name -> list of {scene, x, y} from hotspots."""
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "hotspots.json"))
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf8") as f:
        h = json.load(f)
    out: dict[str, list[dict]] = {}
    for v in (h.get("vendors") or []):
        nm = (v.get("name") or "").lower()
        if not nm:
            continue
        out.setdefault(nm, []).append({"scene": v["scene"], "x": v["x"], "y": v["y"]})
    return out


@app.get("/api/vendors")
def api_vendors(slot: str | None = Query(default=None),
                lang: str = Query(default=DEFAULT_LANG)):
    """Every vendor with the items they sell, localized. Includes current daily stock from save."""
    cat = load_catalog()
    tr = Translator(lang)
    locs = _vendor_locations()
    state = _load_state_for(slot)
    shop_stock = state.shop_stock if state else {}
    out = []
    for s in cat.shops:
        current_stock = shop_stock.get(s.shop_id, {})
        items = []
        for entry in s.items:
            ipid = (entry.get("item") or {}).get("m_PathID", 0)
            it = cat.items_by_path_id.get(ipid)
            if not it:
                continue
            in_stock_qty = current_stock.get(it.item_id, None)
            items.append({
                "item_id": it.item_id,
                "name": tr.item(it.item_id, it.name_id, it.name),
                "buy_copper": it.buy_copper,
                "sell_copper": it.sell_copper,
                "weight": entry.get("weight", 1),
                "always": bool(entry.get("alwaysAppear", 0)),
                "min": entry.get("min", 0),
                "max": entry.get("max", 0),
                "unlimited": bool(entry.get("unlimited", 0)),
                "in_stock": in_stock_qty,  # None = unknown, 0 = sold out, >0 = available
            })
        # Also add any items in the save stock that aren't in the static catalog
        # (rotating daily items)
        static_ids = {i["item_id"] for i in items}
        for iid, qty in current_stock.items():
            if iid not in static_ids:
                it = cat.items_by_id.get(iid)
                if it:
                    items.append({
                        "item_id": iid,
                        "name": tr.item(it.item_id, it.name_id, it.name),
                        "buy_copper": it.buy_copper,
                        "sell_copper": it.sell_copper,
                        "weight": 0,
                        "always": False,
                        "min": 0,
                        "max": 0,
                        "unlimited": False,
                        "in_stock": qty,
                        "daily_special": True,
                    })
        loc = locs.get(s.name.lower(), [])
        out.append({
            "shop_id": s.shop_id,
            "vendor": s.name,
            "shop_type": s.shop_type,
            "item_count": len(items),
            "items": items,
            "locations": loc,
            "has_live_stock": bool(current_stock),
        })
    return out


@app.get("/api/quests")
def api_quests(slot: str | None = Query(default=None),
               lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    state = _load_state_for(slot)
    completed_ids = state.quests_done if state else set()
    active_ids = set(state.quests_active.keys()) if state else set()
    out = []
    for q in cat.quests:
        # Quest nameId is the literal i18n key (e.g. "questNamePorridge")
        name = tr.get(q.name_id) or q.name_id or f"quest #{q.quest_id}"
        desc = tr.get(q.description) or q.description
        state_label = "completed" if q.quest_id in completed_ids else (
                      "active" if q.quest_id in active_ids else "available")
        out.append({
            "quest_id": q.quest_id,
            "name": name,
            "description": desc,
            "required_amount": q.required_amount,
            "is_repeatable": q.is_repeatable,
            "only_halloween": q.only_on_halloween,
            "only_christmas": q.only_on_christmas,
            "recipes_unlocked": q.recipes_unlocked,
            "state": state_label,
        })
    return out


# Perk tree categories: the data file holds the Spanish names. Map to the
# English/i18n keys observed in the I2L term table.
PERK_TREE_KEY = {
    "Recursos": "Resources",
    "Cocina": "Cooking",
    "Servicio": "Service",
    "Granja": "Farming",
    "Cervecería": "Brewing",
    "Cerveceria": "Brewing",
    "Ganadería": "Livestock",
    "Ganaderia": "Livestock",
    "Crianza": "Livestock",
    "Personalidad": "Personality",
    "Comportamiento": "Behaviour",
    "Habilidad": "Ability",
    "Fabricación": "Crafting",
    "Fabricacion": "Crafting",
    "Gestión": "Management",
    "Gestion": "Management",
    "Habilidades": "Skills",
}


@app.get("/api/perks")
def api_perks(lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    def _conv(perks, key_prefix):
        out = []
        for p in perks:
            n = tr.get(f"Perks/{key_prefix}_name_{p.perk_id}") or p.name
            d = tr.get(f"Perks/{key_prefix}_description_{p.perk_id}") or p.description
            tree_key = PERK_TREE_KEY.get(p.perk_tree, p.perk_tree)
            tree_loc = tr.get(tree_key) or tree_key
            out.append({
                "perk_id": p.perk_id,
                "name": n,
                "description": d,
                "tree": tree_loc,
            })
        return out
    return {
        "player": _conv(cat.player_perks, "playerPerk"),
        "employee": _conv(cat.employee_perks, "perk"),
    }


@app.get("/api/talents")
def api_talents(lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    return [{
        "talent_id": t.talent_id,
        "name": tr.get(t.name_id) or t.name,
        "description": tr.get(t.name_id and f"talentDesc_{t.name_id}") or t.description,
    } for t in cat.talents]


@app.get("/api/fish")
def api_fish(lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    out = []
    for f in cat.fishes:
        # Fish IS-A Item — its localization key is Items/item_name_<id>
        name = tr.item(f.fish_id, f.name_id, f.name)
        out.append({
            "fish_id": f.fish_id,
            "name": name,
        })
    return out


SEASON_FLAG = {1: "Spring", 2: "Summer", 4: "Autumn", 8: "Winter"}

@app.get("/api/bushes")
def api_bushes(lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    seen: dict[int, dict] = {}
    for b in cat.bushes:
        d = b.raw
        # harvestedItems is either a single dict {item:{m_PathID}, amount} (Misc*),
        # or a list of those dicts, or just a {m_PathID} (BushHarvest).
        h = d.get("harvestedItems") or d.get("harvestedItem")
        candidates: list[tuple[int, int]] = []  # (item_pid, amount)
        if isinstance(h, dict):
            inner = h.get("item") or h
            ipid = inner.get("m_PathID", 0) if isinstance(inner, dict) else 0
            if ipid:
                candidates.append((ipid, h.get("amount", 1)))
        elif isinstance(h, list):
            for entry in h:
                if isinstance(entry, dict):
                    inner = entry.get("item") or entry
                    ipid = inner.get("m_PathID", 0) if isinstance(inner, dict) else 0
                    if ipid:
                        candidates.append((ipid, entry.get("amount", 1)))

        for ipid, amt in candidates:
            h_item = cat.items_by_path_id.get(ipid)
            if not h_item:
                continue
            name = tr.item(h_item.item_id, h_item.name_id, h_item.name)
            seasons = [SEASON_FLAG[bit] for bit in (1, 2, 4, 8)
                       if d.get("avaliableSeasons", 0) & bit]
            cls = d.get("_class", "BushHarvest")
            rec = {
                "bush_id": h_item.item_id,
                "name": name,
                "harvest_amount_min": d.get("amountMin", amt),
                "harvest_amount_max": d.get("amountMax", amt),
                "days_to_grow": d.get("daysToGrow", 0),
                "probability": d.get("probability", 0),
                "seasons": seasons,
                "kind": cls,
            }
            if h_item.item_id in seen:
                ex = seen[h_item.item_id]
                ex["seasons"] = sorted(set(ex["seasons"]) | set(seasons),
                                       key=lambda s: ["Spring","Summer","Autumn","Winter"].index(s))
                ex["harvest_amount_min"] = min(ex["harvest_amount_min"], rec["harvest_amount_min"])
                ex["harvest_amount_max"] = max(ex["harvest_amount_max"], rec["harvest_amount_max"])
            else:
                seen[h_item.item_id] = rec
    return sorted(seen.values(), key=lambda x: x["name"])


@app.get("/api/hotspots")
def api_hotspots():
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "hotspots.json"))
    if not os.path.exists(p):
        return JSONResponse({"error": "hotspots not extracted — run scripts/dump_hotspots.py"}, status_code=404)
    with open(p, encoding="utf8") as f:
        return json.load(f)


@app.get("/api/reputation")
def api_reputation(lang: str = Query(default=DEFAULT_LANG)):
    cat = load_catalog()
    tr = Translator(lang)
    out = []
    for r in cat.reputations:
        rep_num = r.raw.get("repNumber", 0)
        title = r.raw.get("title", "") or ""
        # Try common i18n key patterns
        name = tr.get(title) or tr.get(f"reputationLevel_{rep_num}") or title or r.name
        # Strip the "N - " prefix on raw fallback
        if name and " - " in name and name.split(" - ", 1)[0].isdigit():
            name = name.split(" - ", 1)[1]
        out.append({
            "rep_id": rep_num,
            "name": name,
            "customers_capacity": r.raw.get("customersCapacity", 0),
            "floor": r.raw.get("floorDisponible", 0),
            "dining_zones": r.raw.get("diningZonesNumber", 0),
            "crafting_zones": r.raw.get("craftingZonesNumber", 0),
            "rented_rooms": r.raw.get("rentedRoomsNumber", 0),
        })
    out.sort(key=lambda x: x["rep_id"])
    return out


@app.get("/api/item/{item_id}")
def api_item(item_id: int, lang: str = Query(default=DEFAULT_LANG),
             slot: str | None = Query(default=None)):
    """Full dossier on any item — sources, uses, vendors, crops, recipes."""
    cat = load_catalog()
    tr = Translator(lang)
    state = _load_state_for(slot)
    planted = state.planted_crop_counts if state else None
    detail = item_detail(item_id, cat, tr, planted_counts=planted)
    if not detail:
        return JSONResponse({"error": "item not found"}, status_code=404)
    return detail


@app.get("/api/items")
def api_items(lang: str = Query(default=DEFAULT_LANG), q: str = Query(default="")):
    """Search all items by name."""
    cat = load_catalog()
    tr = Translator(lang)
    query = q.strip().lower()
    results = []
    for item in cat.items_by_id.values():
        name = tr.item(item.item_id, item.name_id, item.name)
        if query and query not in name.lower():
            continue
        results.append({
            "item_id": item.item_id,
            "name": name,
            "buy_copper": item.buy_copper,
            "sell_copper": item.sell_copper,
            "is_food": item.is_food,
        })
    results.sort(key=lambda x: x["name"])
    return results


@app.get("/api/recipes")
def api_recipes(slot: str | None = Query(default=None),
                lang: str = Query(default=DEFAULT_LANG),
                q: str = Query(default=""),
                group: int | None = Query(default=None)):
    state = _load_state_for(slot)
    cat = load_catalog()
    tr = Translator(lang)
    unlocked = state.unlocked_recipe_ids if state else None
    return list_craftables(cat, tr, unlocked=unlocked, query=q, group_filter=group)


@app.get("/api/recipe/{recipe_id}")
def api_recipe_detail(recipe_id: int,
                      slot: str | None = Query(default=None),
                      lang: str = Query(default=DEFAULT_LANG)):
    state = _load_state_for(slot)
    cat = load_catalog()
    tr = Translator(lang)
    unlocked = state.unlocked_recipe_ids if state else None
    detail = get_recipe_detail(recipe_id, cat, tr, unlocked=unlocked)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _to_jsonable(detail)


@app.get("/api/brew-plan")
def api_brew_plan(slot: str | None = Query(default=None),
                  lang: str = Query(default=DEFAULT_LANG)):
    state = _load_state_for(slot)
    if state is None:
        return JSONResponse({"weeks": []}, status_code=200)
    cat = load_catalog()
    tr = Translator(lang)
    return build_brew_plan_view(state, cat, tr)


@app.get("/api/brewing")
def api_brewing(slot: str | None = Query(default=None),
                lang: str = Query(default=DEFAULT_LANG)):
    state = _load_state_for(slot)
    cat = load_catalog()
    tr = Translator(lang)
    unlocked = state.unlocked_recipe_ids if state else set()
    plans = all_brew_plans(cat, tr, unlocked)
    return _to_jsonable(plans)


# ---- Shared state (per save slot, synced across all connected clients) ----
_carts: dict[str, list[dict]] = {}  # slot_id -> [{item_id, name, qty, buy_copper, vendor}]
_menus: dict[str, list[int]] = {}   # slot_id -> [recipe_id, ...]


@app.get("/api/cart")
def api_cart(slot: str | None = Query(default=None)):
    sid = slot or "default"
    return _carts.get(sid, [])


@app.post("/api/cart")
async def api_cart_update(data: dict):
    sid = str(data.get("slot", "default"))[:50]
    action = data.get("action", "set")
    # Validate slot against real save slots
    valid_slots = {s.slot_id for s in discover_slots()} | {"default"}
    if sid not in valid_slots:
        return JSONResponse({"error": "invalid slot"}, status_code=400)
    if action == "set":
        items = data.get("items", [])[:200]  # cap at 200 items
        _carts[sid] = items
    elif action == "add":
        cart = _carts.setdefault(sid, [])
        if len(cart) >= 200:
            return JSONResponse({"error": "cart full (max 200)"}, status_code=400)
        item = data.get("item", {})
        # Validate item shape
        if not isinstance(item.get("item_id"), int):
            return JSONResponse({"error": "invalid item"}, status_code=400)
        existing = next((i for i in cart if i.get("item_id") == item.get("item_id")), None)
        if existing:
            existing["qty"] = min(existing.get("qty", 0) + item.get("qty", 1), 9999)
        else:
            cart.append({
                "item_id": int(item["item_id"]),
                "name": str(item.get("name", ""))[:100],
                "qty": min(int(item.get("qty", 1)), 9999),
                "buy_copper": int(item.get("buy_copper", 0)),
                "vendor": str(item.get("vendor", ""))[:50],
            })
    elif action == "remove":
        iid = data.get("item_id")
        _carts[sid] = [i for i in _carts.get(sid, []) if i.get("item_id") != iid]
    elif action == "clear":
        _carts[sid] = []
    # Broadcast to all connected clients
    await manager.broadcast({"type": "cart_updated", "slot": sid, "cart": _carts.get(sid, [])})
    return _carts.get(sid, [])


@app.get("/api/menu")
def api_menu(slot: str | None = Query(default=None)):
    sid = slot or "default"
    return _menus.get(sid, [])


@app.post("/api/menu")
async def api_menu_update(data: dict):
    sid = str(data.get("slot", "default"))[:50]
    action = data.get("action", "set")
    if action == "set":
        _menus[sid] = [int(r) for r in data.get("recipes", [])][:100]
    elif action == "add":
        menu = _menus.setdefault(sid, [])
        rid = int(data.get("recipe_id", 0))
        if rid and rid not in menu and len(menu) < 100:
            menu.append(rid)
    elif action == "remove":
        rid = int(data.get("recipe_id", 0))
        menu = _menus.get(sid, [])
        _menus[sid] = [r for r in menu if r != rid]
    elif action == "clear":
        _menus[sid] = []
    await manager.broadcast({"type": "menu_updated", "slot": sid, "menu": _menus.get(sid, [])})
    return _menus.get(sid, [])


async def _try_bridge(path: str, payload: dict, timeout: float = 3.5):
    """Try BepInEx bridge on 8766, return json or None if not running.
    Timeout 3.5s so SyncOnMainThread (2.5-3s) can complete and return actual
    success/failure instead of just 'queued'.
    """
    import urllib.request, urllib.error, json as _j
    for base in _bridge_bases():
        try:
            data = _j.dumps(payload).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode()
                # preserve HTTP semantics: if bridge returns 500 with JSON error, surface it
                j = _j.loads(body) if body else {}
                # attach HTTP status for caller to distinguish ok vs error
                j["_http_status"] = r.status
                return j
        except urllib.error.HTTPError as he:
            try:
                body = he.read().decode()
                j = _j.loads(body) if body else {"error": str(he)}
                j["_http_status"] = he.code
                return j
            except Exception:
                continue
        except Exception:
            continue
    return None


@app.post("/api/cheat/money")
async def api_cheat_money(data: dict, request: Request):
    """Set/add money via the in-game bridge (1g = 10000c). Refuses if the bridge is offline."""
    denied = _write_denied(request)
    if denied:
        return denied
    # Try realtime bridge first
    try:
        copper = int(data.get("copper", data.get("amount", 0)))
    except Exception:
        return JSONResponse({"error": "copper must be int"}, status_code=400)
    action = str(data.get("action", "set"))
    # Bridge expects absolute copper for set, or delta for add via addMoney
    bridge_payload = {"copper": copper, "action": action}
    # Try bridge first (realtime sync — now returns actual result, not just queued)
    try:
        import urllib.request, urllib.error, json as _j
        for base in _bridge_bases():
            try:
                bdata = _j.dumps({"copper": copper, "action": action}).encode()
                req = urllib.request.Request(base + "/addMoney", data=bdata, headers={"Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as r:
                    body = r.read().decode()
                    j = _j.loads(body) if body else {}
                    if j.get("ok"):
                        return {"bridge": True, "realtime": True, **j}
                    # if returned queued (202) still realtime, surface to UI
                    if j.get("queued"):
                        return {"bridge": True, "realtime": True, "queued": True, **j}
            except urllib.error.HTTPError as he:
                try:
                    j = _j.loads(he.read().decode())
                    # surface actual error (e.g., not enough money would be 500)
                    if j.get("error"):
                        return JSONResponse({"error": j.get("error"), "bridge": True}, status_code=he.code)
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass

    # Bridge not running → refuse. The planner NEVER mutates the game through
    # the save file; the in-game bridge is the only write channel to live state.
    return JSONResponse(
        {
            "error": "game not running — PlannerBridge offline; cannot mutate live state. Start the game with the PlannerBridge plugin, or use save-only read mode.",
            "bridge": False,
        },
        status_code=503,
    )


@app.post("/api/cheat/seed")
async def api_cheat_seed(data: dict, request: Request):
    """Add seeds via the in-game bridge (sync). Refuses if the bridge is offline."""
    denied = _write_denied(request)
    if denied:
        return denied
    try:
        item_id = int(data.get("itemId", data.get("item_id", data.get("seedId", 0))))
        count = int(data.get("count", data.get("amount", data.get("qty", 10))))
    except Exception:
        return JSONResponse({"error": "itemId/count must be int"}, status_code=400)
    if not item_id:
        return JSONResponse({"error": "itemId required"}, status_code=400)
    if count < 1: count = 1
    if count > 999: count = 999
    bridged = await _try_bridge("/addItem", {"itemId": item_id, "count": count})
    if bridged is not None:
        if bridged.get("ok"):
            return {"bridge": True, "realtime": True, **bridged}
        # bridge returned error JSON (e.g., no inventory, timeout, invalid id)
        if "_http_status" in bridged and bridged.get("_http_status", 200) >= 400:
            err = bridged.get("error") or bridged.get("result") or "bridge error"
            return JSONResponse({"error": f"bridge: {err}", "bridge": True, "detail": bridged}, status_code=bridged["_http_status"])
        # 202 queued
        if bridged.get("queued"):
            return {"bridge": True, "realtime": True, "queued": True, **bridged}
    # Bridge not running → refuse. The planner NEVER mutates the game through
    # the save file; the in-game bridge is the only write channel to live state.
    return JSONResponse(
        {
            "error": "game not running — PlannerBridge offline; cannot mutate live state. Start the game with the PlannerBridge plugin, or use save-only read mode.",
            "bridge": False,
        },
        status_code=503,
    )


@app.post("/api/shop/buy")
async def api_shop_buy(data: dict, request: Request):
    """Remote buy: add item to inventory and subtract money via the in-game bridge. Refuses if the bridge is offline."""
    denied = _write_denied(request)
    if denied:
        return denied
    try:
        item_id = int(data.get("itemId", data.get("item_id", 0)))
        count = int(data.get("count", 1))
        price = int(data.get("price", data.get("buy_copper", 0)))
    except Exception:
        return JSONResponse({"error": "itemId/count/price must be int"}, status_code=400)
    if not item_id or count < 1:
        return JSONResponse({"error": "itemId and count required"}, status_code=400)
    bridged = await _try_bridge("/shop/buy", {"itemId": item_id, "count": count, "price": price})
    if bridged is not None:
        if bridged.get("ok"):
            return {"bridge": True, "realtime": True, **bridged}
        if bridged.get("queued"):
            return {"bridge": True, "realtime": True, "queued": True, **bridged}
        status = int(bridged.get("_http_status", 500)) if isinstance(bridged.get("_http_status"), int) else 500
        if status >= 400:
            err = bridged.get("error") or bridged.get("result") or "bridge buy failed"
            # Mirror gold cheat UX: show why money/inventory failed (e.g., need X have Y, or load save)
            return JSONResponse({"error": f"buy failed: {err}", "bridge": True, "detail": bridged}, status_code=status)
    # Bridge not running → refuse. The planner NEVER mutates the game through
    # the save file; the in-game bridge is the only write channel to live state.
    return JSONResponse(
        {
            "error": "game not running — PlannerBridge offline; cannot mutate live state. Start the game with the PlannerBridge plugin, or use save-only read mode.",
            "bridge": False,
        },
        status_code=503,
    )


@app.post("/api/shop/sell")
async def api_shop_sell(data: dict, request: Request):
    """Remote sell: remove item and add money via the in-game bridge. Refuses if the bridge is offline."""
    denied = _write_denied(request)
    if denied:
        return denied
    try:
        item_id = int(data.get("itemId", data.get("item_id", 0)))
        count = int(data.get("count", 1))
        price = int(data.get("price", data.get("sell_copper", 0)))
    except Exception:
        return JSONResponse({"error": "itemId/count/price must be int"}, status_code=400)
    if not item_id or count < 1:
        return JSONResponse({"error": "itemId and count required"}, status_code=400)
    bridged = await _try_bridge("/shop/sell", {"itemId": item_id, "count": count, "price": price})
    if bridged is not None:
        if bridged.get("ok"):
            return {"bridge": True, "realtime": True, **bridged}
        if bridged.get("queued"):
            return {"bridge": True, "realtime": True, "queued": True, **bridged}
        status = int(bridged.get("_http_status", 500)) if isinstance(bridged.get("_http_status"), int) else 500
        if status >= 400:
            err = bridged.get("error") or bridged.get("result") or "bridge sell failed"
            return JSONResponse({"error": f"sell failed: {err}", "bridge": True, "detail": bridged}, status_code=status)
    # Bridge not running → refuse. The planner NEVER mutates the game through
    # the save file; the in-game bridge is the only write channel to live state.
    return JSONResponse(
        {
            "error": "game not running — PlannerBridge offline; cannot mutate live state. Start the game with the PlannerBridge plugin, or use save-only read mode.",
            "bridge": False,
        },
        status_code=503,
    )


# ---------- Bridge -> planner feedback (graphical realtime) -----------------
# PlannerBridge pushes events here after each successful AddItem/Money/Shop op.
# We rebroadcast as bridge_event over the same /ws so the web UI can show a
# toast/pulse immediately (<200ms) without waiting for the save file to flush
# to disk (which takes 0.5-2s even in single-file mode).

@app.post("/api/bridge/push")
async def api_bridge_push(data: dict, request: Request):
    # Anti-spoof: in share mode only the local bridge itself may push (it never
    # leaves this machine; remote/tunneled callers are rejected outright).
    # Default mode is bound to 127.0.0.1 anyway, so no check needed there.
    if _share_mode() and not _is_local_client(request):
        return JSONResponse({"error": "bridge push is local-only"}, status_code=403)
    # Validate minimal shape
    t = str(data.get("type", "bridge_event"))
    ev = data.get("event", data)
    # Broadcast to all web clients for graphical feedback
    await manager.broadcast({"type": "bridge_event", "event": ev, "received_at": __import__("time").time()})
    # Also log for debugging
    print(f"[bridge push] {ev.get('type') if isinstance(ev, dict) else t} -> broadcast bridge_event", flush=True)
    return {"ok": True, "broadcast": True}

@app.post("/api/bridge/heartbeat")
async def api_bridge_heartbeat(data: dict, request: Request):
    """Bridge liveness heartbeat (G0). The in-game bridge POSTs every ~2s
    while the game runs; freshness here is the sole definition of live mode.
    Local-only in share mode (remote callers may not fake liveness)."""
    if _share_mode() and not _is_local_client(request):
        return JSONResponse({"error": "heartbeat is local-only"}, status_code=403)
    global _bridge_last_beat, _bridge_last_info
    _bridge_last_beat = time.time()
    if isinstance(data, dict):
        _bridge_last_info = {
            k: data.get(k) for k in ("version", "uptime_s", "spawned_planner", "planner_restarts")
            if k in data
        }
    return {"ok": True, "live": True}


@app.get("/api/bridge/status")
async def api_bridge_status():
    # Proxy to BepInEx bridge so web UI can poll a single localhost:8765 endpoint.
    # Merges planner-side heartbeat state: "live" is derived from heartbeat
    # freshness (G0), not just from this proxy call succeeding.
    import urllib.request, json as _j
    live = _bridge_live()
    heartbeat = {
        "live": live,
        "heartbeat_age_s": round(time.time() - _bridge_last_beat, 1) if _bridge_last_beat else None,
        "heartbeat_timeout_s": _heartbeat_timeout_s(),
        **({"bridge_info": _bridge_last_info} if _bridge_last_info else {}),
    }
    for base in _bridge_bases():
        try:
            with urllib.request.urlopen(base + "/bridge/status", timeout=1.0) as r:
                body = r.read().decode()
                j = _j.loads(body) if body else {}
                # Planner-side fields win: the bridge's own "bridge":"planner"
                # identity string must not clobber the boolean contract.
                return {**j, "bridge": True, "realtime": True, "url": base, **heartbeat}
        except Exception:
            continue
    return JSONResponse(
        {"bridge": False, "error": "bridge not running (restart TR, check BepInEx/LogOutput.log)", **heartbeat},
        status_code=503,
    )

@app.get("/api/bridge/events")
async def api_bridge_events():
    import urllib.request, json as _j
    for base in _bridge_bases():
        try:
            with urllib.request.urlopen(base + "/bridge/events", timeout=1.2) as r:
                return _j.loads(r.read().decode())
        except Exception:
            continue
    return JSONResponse({"error": "bridge not running"}, status_code=503)

@app.get("/api/debug/saves")
def api_debug_saves():
    """Verbose debug: show single-file watcher state + save stats."""
    import time as _t
    root = saves_root()
    slot = get_slot("File_1")
    info: dict = {
        "single_file_mode": _single_file_mode,
        "saves_root": root,
        "watcher_active": _observer is not None and getattr(_observer, "is_alive", lambda: False)(),
        "slot": None,
        "all_slots_scanned": len(discover_slots(single_file_only=False)),
    }
    if slot:
        try:
            st = os.stat(slot.latest_file)
            info["slot"] = {
                "slot_id": slot.slot_id,
                "folder": slot.folder,
                "latest_file": slot.latest_file,
                "mtime": slot.mtime,
                "mtime_str": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(slot.mtime)),
                "size": st.st_size,
                "age_s": _t.time() - slot.mtime,
            }
        except Exception as e:
            info["slot_error"] = str(e)
    return info


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # ignore inbound, just keep it alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        manager.disconnect(ws)


# ---------- Static UI -------------------------------------------------------
# Prefer the built React app if it exists; otherwise serve a vanilla HTML fallback.

from fastapi.responses import HTMLResponse
from planner.server.static import INDEX_HTML

ICONS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "icons"))
if os.path.isdir(ICONS_DIR):
    app.mount("/icons", StaticFiles(directory=ICONS_DIR), name="icons")

MAPS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "maps"))
if os.path.isdir(MAPS_DIR):
    app.mount("/maps", StaticFiles(directory=MAPS_DIR), name="maps")


@app.get("/api/maps")
def api_maps():
    p = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "maps.json"))
    if not os.path.exists(p):
        return JSONResponse({}, status_code=200)
    with open(p, encoding="utf8") as f:
        return json.load(f)

WEB_DIST = os.path.join(os.path.dirname(__file__), "..", "web", "dist")
WEB_DIST = os.path.abspath(WEB_DIST)
if os.path.isdir(WEB_DIST) and os.path.isdir(os.path.join(WEB_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(WEB_DIST, "assets")), name="assets")

    @app.get("/")
    def index_react():
        return FileResponse(os.path.join(WEB_DIST, "index.html"))
else:
    @app.get("/", response_class=HTMLResponse)
    def index_vanilla():
        return INDEX_HTML


# ---------- entry ------------------------------------------------------------

def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Travellers Rest Planner")
    parser.add_argument("--share", action="store_true",
                        help="Open to your network so multiplayer friends can view")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tunnel", action="store_true",
                        help="Create a public tunnel via ngrok (requires ngrok installed)")
    args = parser.parse_args()

    host = "0.0.0.0" if args.share or args.tunnel else "127.0.0.1"

    if args.share or args.tunnel:
        # Gate game-mutating endpoints behind the per-run SHARE_TOKEN (SEC-1):
        # guests open the share URL (which carries #t=<token>); the host's own
        # direct-localhost browser is exempt.
        os.environ["TR_SHARE"] = "1"
        print(f"  Share token (writes): {SHARE_TOKEN}")

    if args.share:
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        _allowed_origins.append(f"http://{local_ip}:{args.port}")
        print(f"\n  Sharing on your local network!")
        print(f"  Give this to your friends on the same WiFi/LAN")
        print(f"  (link carries the write token):")
        print(f"  http://{local_ip}:{args.port}/#t={SHARE_TOKEN}\n")

    if args.tunnel:
        import threading
        def run_tunnel():
            try:
                from pyngrok import ngrok
                print("\n  Setting up public tunnel (auto-installs ngrok if needed)...")
                tunnel = ngrok.connect(args.port, "http")
                # Add the tunnel URL to allowed CORS origins
                _allowed_origins.append(tunnel.public_url)
                _allowed_origins.append(tunnel.public_url.replace("https://", "http://"))
                print(f"\n  ==========================================")
                print(f"  Public share link — send to your friends")
                print(f"  (link carries the write token):")
                print(f"  {tunnel.public_url}/#t={SHARE_TOKEN}")
                print(f"  ==========================================\n")
            except ImportError:
                print("\n  pyngrok not installed. Run: pip install pyngrok")
                print("  or use --share for LAN-only sharing\n")
            except Exception as e:
                print(f"\n  tunnel failed: {e}\n")
        threading.Thread(target=run_tunnel, daemon=True).start()

    uvicorn.run("planner.server.app:app", host=host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
