"""Render per-scene tilemap PNGs.

For each level scene, find every Tilemap component, walk its tile placements,
look up the tile sprites via the level's external file references, and
composite into a per-scene PNG.

Tilemap typetree (Unity 2022 / Travellers Rest):
  m_Tiles[]:           tuples of (Vector3Int position, TileChangeData)
  m_TileSpriteArray[]: list of {m_RefCount, m_Data:{m_FileID, m_PathID}}
  m_TileMatrixArray[]: list of {m_RefCount, m_Data: 4x4 matrix}
  m_TileColorArray[]:  list of {m_RefCount, m_Data: rgba}
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from PIL import Image

import UnityPy
UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0"

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from planner.gamepath import find_game_data_dir; GAME = find_game_data_dir()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(ROOT, "data", "maps")
META = os.path.join(ROOT, "data", "maps.json")
os.makedirs(OUT, exist_ok=True)

PPU = 16
MAX_OUTPUT_DIM = 4096
MAX_OUTPUT_PIXELS = 60_000_000


def split_tile(entry):
    if isinstance(entry, tuple) and len(entry) == 2:
        return entry[0], entry[1]
    if isinstance(entry, dict):
        return entry.get("first"), entry.get("second", entry)
    return None, None


def load_sprite_image(obj):
    try:
        d = obj.read()
        img = d.image
        if img is None:
            return None
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        return img
    except Exception:
        return None


def ref_path_id(ref):
    return ref.get("m_PathID", 0) if isinstance(ref, dict) else 0


def object_data(obj):
    try:
        return obj.read_typetree()
    except Exception:
        return {}


def game_object_info(level_objs):
    """Build component ownership and active/name indexes for a scene."""
    owners = {}
    names = {}
    active = {}
    transforms = {}
    for obj in level_objs.values():
        if obj.type.name != "GameObject":
            continue
        data = object_data(obj)
        names[obj.path_id] = data.get("m_Name", f"GameObject-{obj.path_id}")
        active[obj.path_id] = bool(data.get("m_IsActive", 1))
        for comp in data.get("m_Component") or []:
            ref = comp.get("component", comp) if isinstance(comp, dict) else {}
            pid = ref_path_id(ref)
            if pid:
                owners[pid] = obj.path_id
                child = level_objs.get(pid)
                if child is not None and child.type.name in ("Transform", "RectTransform"):
                    transforms[obj.path_id] = pid
    return owners, names, active, transforms


def transform_info(level_objs, owners, transforms):
    """Return parent GameObjects and accumulated world positions."""
    parents = {}
    local_positions = {}
    for go_id, transform_id in transforms.items():
        data = object_data(level_objs[transform_id])
        pos = data.get("m_LocalPosition") or {}
        local_positions[go_id] = (float(pos.get("x", 0)), float(pos.get("y", 0)))
        father_id = ref_path_id(data.get("m_Father") or {})
        parents[go_id] = owners.get(father_id) if father_id else None

    world_cache = {}

    def world_position(go_id, seen=None):
        if go_id in world_cache:
            return world_cache[go_id]
        seen = seen or set()
        if go_id in seen:
            return local_positions.get(go_id, (0.0, 0.0))
        seen.add(go_id)
        x, y = local_positions.get(go_id, (0.0, 0.0))
        parent = parents.get(go_id)
        if parent:
            px, py = world_position(parent, seen)
            x += px
            y += py
        world_cache[go_id] = (x, y)
        return x, y

    return parents, world_position


def active_in_hierarchy(go_id, parents, active):
    seen = set()
    while go_id and go_id not in seen:
        seen.add(go_id)
        if not active.get(go_id, True):
            return False
        go_id = parents.get(go_id)
    return True


def object_path(go_id, parents, names):
    parts = []
    seen = set()
    while go_id and go_id not in seen:
        seen.add(go_id)
        parts.append(names.get(go_id, str(go_id)))
        go_id = parents.get(go_id)
    return "/".join(reversed(parts))


def choose_ppu(width, height, requested=PPU):
    ppu = requested
    while ppu > 1 and (
        width * ppu > MAX_OUTPUT_DIM
        or height * ppu > MAX_OUTPUT_DIM
        or width * height * ppu * ppu > MAX_OUTPUT_PIXELS
    ):
        ppu //= 2
    return ppu


def decode_tilemap_sprites(tilemaps, resolve_ref):
    cache = {}
    decoded_by_object = {}
    for tm_info in tilemaps:
        decoded = []
        for ref_wrap in tm_info["data"].get("m_TileSpriteArray") or []:
            data = ref_wrap.get("m_Data") if isinstance(ref_wrap, dict) else None
            if not data:
                decoded.append(None)
                continue
            key = (data.get("m_FileID", 0), data.get("m_PathID", 0))
            if key not in cache:
                obj = resolve_ref(*key)
                cache[key] = load_sprite_image(obj) if obj is not None and obj.type.name == "Sprite" else None
            decoded.append(cache[key])
        decoded_by_object[tm_info["object"].path_id] = decoded
    return decoded_by_object


def largest_connected_cells(tilemaps):
    occupied = set()
    for tm in tilemaps:
        for entry in tm["data"].get("m_Tiles") or []:
            pos, _ = split_tile(entry)
            if isinstance(pos, dict):
                occupied.add((int(pos.get("x", 0)), int(pos.get("y", 0))))
    largest = set()
    remaining = set(occupied)
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            x, y = stack.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        if len(component) > len(largest):
            largest = component
    return largest


def primary_world_cluster(tilemaps, nearby_distance=50.0):
    """Keep the main world-space cluster plus substantial nearby chunks.

    Aggregate scenes can contain staging fragments hundreds of units away.
    Nearby disconnected chunks are still valid parts of the same map.
    """
    occupied = set()
    for tm in tilemaps:
        grid = tm["grid"]
        ox, oy = tm["world_position"]
        for entry in tm["data"].get("m_Tiles") or []:
            pos, _ = split_tile(entry)
            if isinstance(pos, dict):
                x = ox + float(pos.get("x", 0)) * grid["cell_x"]
                y = oy + float(pos.get("y", 0)) * grid["cell_y"]
                occupied.add((round(x * 2), round(y * 2)))

    components = []
    remaining = set(occupied)
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            x, y = stack.pop()
            for neighbor in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    if not components:
        return set()

    primary = max(components, key=len)
    min_size = max(64, round(len(primary) * 0.01))
    pmin_x = min(x for x, _ in primary)
    pmax_x = max(x for x, _ in primary)
    pmin_y = min(y for _, y in primary)
    pmax_y = max(y for _, y in primary)
    selected = set(primary)
    max_gap = round(nearby_distance * 2)
    for component in components:
        if component is primary or len(component) < min_size:
            continue
        min_x = min(x for x, _ in component)
        max_x = max(x for x, _ in component)
        min_y = min(y for _, y in component)
        max_y = max(y for _, y in component)
        gap_x = max(0, pmin_x - max_x, min_x - pmax_x)
        gap_y = max(0, pmin_y - max_y, min_y - pmax_y)
        if max(gap_x, gap_y) <= max_gap:
            selected.update(component)
    return selected


def composite_sprite_renderers(canvas, renderers, resolve_ref, bounds, pixels_per_world, root_path):
    min_x, min_y, max_x, max_y = bounds
    cache = {}
    drawn = 0
    for renderer in sorted(renderers, key=lambda r: (r["layer"], r["order"], -r["world_position"][1])):
        if root_path and not renderer["path"].startswith(root_path + "/"):
            continue
        ref = renderer["data"].get("m_Sprite") or {}
        key = (ref.get("m_FileID", 0), ref.get("m_PathID", 0))
        if not key[1]:
            continue
        if key not in cache:
            obj = resolve_ref(*key)
            if obj is None or obj.type.name != "Sprite":
                cache[key] = None
            else:
                sprite_data = object_data(obj)
                image = load_sprite_image(obj)
                pivot = sprite_data.get("m_Pivot") or {"x": 0.5, "y": 0.5}
                ppu = float(sprite_data.get("m_PixelsToUnits", 100) or 100)
                cache[key] = (image, float(pivot.get("x", 0.5)), float(pivot.get("y", 0.5)), ppu)
        sprite = cache[key]
        if not sprite or sprite[0] is None:
            continue
        image, pivot_x, pivot_y, sprite_ppu = sprite
        world_w = image.width / sprite_ppu
        world_h = image.height / sprite_ppu
        x, y = renderer["world_position"]
        left = x - pivot_x * world_w
        bottom = y - pivot_y * world_h
        right = left + world_w
        top = bottom + world_h
        if right <= min_x or left >= max_x or top <= min_y or bottom >= max_y:
            continue
        width = max(1, round(world_w * pixels_per_world))
        height = max(1, round(world_h * pixels_per_world))
        image = image.resize((width, height), Image.Resampling.NEAREST)
        if renderer["data"].get("m_FlipX"):
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if renderer["data"].get("m_FlipY"):
            image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        px = round((left - min_x) * pixels_per_world)
        py = round((max_y - top) * pixels_per_world)
        if px < 0 or py < 0 or px + width > canvas.width or py + height > canvas.height:
            continue
        canvas.alpha_composite(image, (px, py))
        drawn += 1
    return drawn


def render_grid_regions(scene_name, tilemaps, sprite_renderers, resolve_ref):
    if scene_name not in ("level2", "level12"):
        return None
    grouped = defaultdict(list)
    for tm in tilemaps:
        path = tm.get("path", "").lower()
        nonvisual = any(part in path for part in (
            "functionaltilemap", "/functional", "/location", "/material", "/zones",
        ))
        if tm["grid"] and tm["active"] and not nonvisual and "gametilemaps/" not in path:
            grouped[tm["grid"]["path_id"]].append(tm)
    if not grouped or (scene_name == "level2" and len(grouped) <= 1):
        return None
    if scene_name == "level12":
        grouped = {"city": [tm for group in grouped.values() for tm in group]}

    decoded = decode_tilemap_sprites(tilemaps, resolve_ref)
    regions = []
    for grid_id, group in grouped.items():
        grid = group[0]["grid"]
        allowed_cells = None
        allowed_world_cells = None
        if grid["path"] == "TavernMap/Tilemaps":
            allowed_cells = largest_connected_cells(group)
        elif scene_name == "level12":
            allowed_world_cells = primary_world_cluster(group)
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        tile_count = 0
        for tm in group:
            tm_grid = tm["grid"]
            ox, oy = tm["world_position"]
            for entry in tm["data"].get("m_Tiles") or []:
                pos, _ = split_tile(entry)
                if not isinstance(pos, dict):
                    continue
                cell = (int(pos.get("x", 0)), int(pos.get("y", 0)))
                if allowed_cells is not None and cell not in allowed_cells:
                    continue
                x = ox + float(pos.get("x", 0)) * tm_grid["cell_x"]
                y = oy + float(pos.get("y", 0)) * tm_grid["cell_y"]
                if allowed_world_cells is not None and (round(x * 2), round(y * 2)) not in allowed_world_cells:
                    continue
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x + tm_grid["cell_x"])
                max_y = max(max_y, y + tm_grid["cell_y"])
                tile_count += 1
        if not tile_count:
            continue

        width_world = max_x - min_x
        height_world = max_y - min_y
        pixels_per_world = choose_ppu(width_world, height_world, requested=32)
        pixel_w = max(1, min(MAX_OUTPUT_DIM, round(width_world * pixels_per_world)))
        pixel_h = max(1, min(MAX_OUTPUT_DIM, round(height_world * pixels_per_world)))
        canvas = Image.new("RGBA", (pixel_w, pixel_h), (0, 0, 0, 0))
        drawn = 0
        for tm in group:
            tm_grid = tm["grid"]
            ox, oy = tm["world_position"]
            sprites = decoded.get(tm["object"].path_id, [])
            for entry in tm["data"].get("m_Tiles") or []:
                pos, data = split_tile(entry)
                if not isinstance(pos, dict) or not isinstance(data, dict):
                    continue
                cell = (int(pos.get("x", 0)), int(pos.get("y", 0)))
                if allowed_cells is not None and cell not in allowed_cells:
                    continue
                world_x = ox + float(pos.get("x", 0)) * tm_grid["cell_x"]
                world_y = oy + float(pos.get("y", 0)) * tm_grid["cell_y"]
                if allowed_world_cells is not None and (round(world_x * 2), round(world_y * 2)) not in allowed_world_cells:
                    continue
                sprite_index = data.get("m_TileSpriteIndex", -1)
                if sprite_index < 0 or sprite_index >= len(sprites) or sprites[sprite_index] is None:
                    continue
                img = sprites[sprite_index]
                cell_w = max(1, round(tm_grid["cell_x"] * pixels_per_world))
                cell_h = max(1, round(tm_grid["cell_y"] * pixels_per_world))
                if img.size != (cell_w, cell_h):
                    img = img.resize((cell_w, cell_h), Image.Resampling.NEAREST)
                px = round((world_x - min_x) * pixels_per_world)
                py = round((max_y - world_y - tm_grid["cell_y"]) * pixels_per_world)
                if 0 <= px <= pixel_w - cell_w and 0 <= py <= pixel_h - cell_h:
                    canvas.alpha_composite(img, (px, py))
                    drawn += 1
        root_path = grid["path"].split("/", 1)[0]
        drawn_sprites = composite_sprite_renderers(
            canvas, sprite_renderers, resolve_ref,
            (min_x, min_y, max_x, max_y), pixels_per_world, root_path,
        )
        if not drawn:
            continue

        image_name = f"{scene_name}--grid-{grid_id}.png"
        canvas.save(os.path.join(OUT, image_name), "PNG", optimize=True)
        label = grid["path"].replace("/TilemapsSpring", "").replace("/Tilemaps", "")
        if grid["path"] == "TavernMap/Tilemaps":
            label = "Tavern exterior"
        elif scene_name == "level12":
            label = "City"
        regions.append({
            "id": f"grid-{grid_id}",
            "label": label,
            "path": grid["path"],
            "image": image_name,
            "coordinate_space": "world",
            "width": pixel_w,
            "height": pixel_h,
            "pixels_per_world_unit": pixels_per_world,
            "world_min_x": min_x,
            "world_min_y": min_y,
            "world_max_x": max_x,
            "world_max_y": max_y,
            "drawn_tiles": drawn,
            "drawn_sprites": drawn_sprites,
        })

    regions.sort(key=lambda r: ("TavernMap/Tilemaps" not in r["path"], r["label"]))
    return {
        "scene": scene_name,
        "coordinate_space": "world",
        "regions": regions,
        "drawn_tiles": sum(r["drawn_tiles"] for r in regions),
    } if regions else None


def render_scene(env, scene_name, all_objs_by_pid) -> dict | None:
    """env is the FULL game env (so externals resolve). all_objs_by_pid is
    a global path_id index for fast lookup."""
    # Locate the level's SerializedFile inside the env
    level_sf = None
    for cab, sf in env.files.items():
        if os.path.basename(str(cab)).lower() == scene_name.lower() and hasattr(sf, "objects"):
            level_sf = sf
            break
    if level_sf is None:
        return None

    # SerializedFile.objects is already dict[path_id, ObjectReader]
    level_objs = level_sf.objects

    # The external file list is on the SerializedFile. file_id N (1-indexed)
    # maps to externals[N-1].path which we then look up in env.files.
    externals = getattr(level_sf, "externals", []) or []
    ext_files = []
    for ext in externals:
        # ext.path is like "archive:/CAB-xxxx/sharedassets1.assets"
        # We need to find the matching env.files entry
        ext_name = os.path.basename(
            getattr(ext, "name", None) or getattr(ext, "path", "") or ""
        )
        match = None
        for cab, sf in env.files.items():
            if ext_name and os.path.basename(str(cab)) == ext_name and hasattr(sf, "objects"):
                match = sf
                break
        ext_files.append(match)

    def resolve_ref(file_id, path_id):
        if not path_id:
            return None
        if file_id == 0:
            return level_objs.get(path_id)
        idx = file_id - 1
        if idx < 0 or idx >= len(ext_files):
            return None
        sf = ext_files[idx]
        if sf is None:
            return None
        return sf.objects.get(path_id)

    owners, names, active, transforms = game_object_info(level_objs)
    parents, world_position = transform_info(level_objs, owners, transforms)

    grids = {}
    for obj in level_objs.values():
        if obj.type.name != "Grid":
            continue
        go_id = owners.get(obj.path_id)
        data = object_data(obj)
        cell = data.get("m_CellSize") or {}
        if go_id:
            grids[go_id] = {
                "path_id": obj.path_id,
                "go_id": go_id,
                "cell_x": float(cell.get("x", 1) or 1),
                "cell_y": float(cell.get("y", 1) or 1),
                "path": object_path(go_id, parents, names),
            }

    def nearest_grid(go_id):
        seen = set()
        while go_id and go_id not in seen:
            seen.add(go_id)
            if go_id in grids:
                return grids[go_id]
            go_id = parents.get(go_id)
        return None

    # Walk tilemaps
    tilemaps = []
    for o in level_sf.objects.values():
        if o.type.name != "Tilemap":
            continue
        try:
            t = o.read_typetree()
        except Exception:
            continue
        if not t.get("m_Tiles"):
            continue
        go_id = owners.get(o.path_id)
        tilemaps.append({
            "object": o,
            "data": t,
            "go_id": go_id,
            "path": object_path(go_id, parents, names) if go_id else "",
            "grid": nearest_grid(go_id),
            "active": active_in_hierarchy(go_id, parents, active),
            "world_position": world_position(go_id) if go_id else (0.0, 0.0),
        })

    if not tilemaps:
        return None

    sprite_renderers = []
    for obj in level_objs.values():
        if obj.type.name != "SpriteRenderer":
            continue
        data = object_data(obj)
        go_id = owners.get(obj.path_id)
        if not go_id or not data.get("m_Enabled", 1) or not active_in_hierarchy(go_id, parents, active):
            continue
        sprite_renderers.append({
            "object": obj,
            "data": data,
            "path": object_path(go_id, parents, names),
            "world_position": world_position(go_id),
            "layer": int(data.get("m_SortingLayer", 0) or 0),
            "order": int(data.get("m_SortingOrder", 0) or 0),
        })

    regional = render_grid_regions(scene_name, tilemaps, sprite_renderers, resolve_ref)
    if regional:
        return regional

    # Compute global bounds
    min_x = min_y = 10**9
    max_x = max_y = -10**9
    total = 0
    for tm in tilemaps:
        t = tm["data"]
        for entry in (t.get("m_Tiles") or []):
            pos, _data = split_tile(entry)
            if not isinstance(pos, dict):
                continue
            x = pos.get("x", 0)
            y = pos.get("y", 0)
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
            total += 1
    if total == 0 or min_x > max_x:
        return None

    width_t = max_x - min_x + 1
    height_t = max_y - min_y + 1
    ppu = choose_ppu(width_t, height_t)
    pixel_w = min(MAX_OUTPUT_DIM, width_t * ppu)
    pixel_h = min(MAX_OUTPUT_DIM, height_t * ppu)

    canvas = Image.new("RGBA", (pixel_w, pixel_h), (0, 0, 0, 0))
    sprite_cache: dict[tuple, Image.Image | None] = {}
    drawn = 0

    for tm_idx, tm_info in enumerate(tilemaps):
        tm_obj = tm_info["object"]
        tm = tm_info["data"]
        sprite_array = tm.get("m_TileSpriteArray") or []
        # Pre-decode sprites that this tilemap uses
        decoded = []
        for ref_wrap in sprite_array:
            data = ref_wrap.get("m_Data") if isinstance(ref_wrap, dict) else None
            if not data:
                decoded.append(None)
                continue
            fid = data.get("m_FileID", 0)
            pid = data.get("m_PathID", 0)
            key = (fid, pid)
            if key in sprite_cache:
                decoded.append(sprite_cache[key])
                continue
            obj = resolve_ref(fid, pid)
            if obj is None or obj.type.name != "Sprite":
                sprite_cache[key] = None
                decoded.append(None)
                continue
            img = load_sprite_image(obj)
            sprite_cache[key] = img
            decoded.append(img)

        for entry in (tm.get("m_Tiles") or []):
            pos, data = split_tile(entry)
            if not isinstance(pos, dict) or not isinstance(data, dict):
                continue
            si = data.get("m_TileSpriteIndex", -1)
            if si < 0 or si >= len(decoded):
                continue
            img = decoded[si]
            if img is None:
                continue
            tx = pos.get("x", 0) - min_x
            ty = pos.get("y", 0) - min_y
            px = tx * ppu
            py = (height_t - ty - 1) * ppu
            if px < 0 or py < 0 or px + img.width > pixel_w or py + img.height > pixel_h:
                continue
            try:
                # Scale sprite if our ppu is reduced
                if ppu != img.width and img.width > ppu:
                    img2 = img.resize((ppu, ppu), Image.Resampling.NEAREST)
                else:
                    img2 = img
                canvas.alpha_composite(img2, (int(px), int(py)))
                drawn += 1
            except Exception:
                pass

    if drawn == 0:
        return None

    out_path = os.path.join(OUT, f"{scene_name}.png")
    canvas.save(out_path, "PNG", optimize=True)
    return {
        "scene": scene_name,
        "width": pixel_w,
        "height": pixel_h,
        "ppu": ppu,
        "world_min_x": min_x,
        "world_min_y": min_y,
        "world_max_x": max_x,
        "world_max_y": max_y,
        "drawn_tiles": drawn,
    }


def main():
    hotspots_path = os.path.join(ROOT, "data", "hotspots.json")
    target = set()
    if os.path.exists(hotspots_path):
        with open(hotspots_path, encoding="utf8") as f:
            h = json.load(f)
        for k in ("trees", "foraging", "fishing", "vendors"):
            for x in (h.get(k) or []):
                target.add(x["scene"])
    if not target:
        target = {f"level{i}" for i in range(28)}

    print(f"[maps] loading full game env...", file=sys.stderr)
    env = UnityPy.load(GAME)
    print(f"[maps] {len(env.files)} files loaded", file=sys.stderr)

    metas = {}
    for scene in sorted(target):
        try:
            meta = render_scene(env, scene, None)
        except Exception as e:
            print(f"  fail {scene}: {e}", file=sys.stderr)
            continue
        if meta:
            metas[scene] = meta
            if meta.get("regions"):
                print(f"  ✓ {scene}: {len(meta['regions'])} regions ({meta['drawn_tiles']} tiles)", file=sys.stderr)
            else:
                print(f"  ✓ {scene}: {meta['width']}x{meta['height']} ppu={meta['ppu']} ({meta['drawn_tiles']} tiles)", file=sys.stderr)
        else:
            print(f"  · {scene}: empty", file=sys.stderr)

    with open(META, "w", encoding="utf8") as f:
        json.dump(metas, f, indent=2)
    print(f"[maps] wrote {len(metas)} maps", file=sys.stderr)


if __name__ == "__main__":
    main()
