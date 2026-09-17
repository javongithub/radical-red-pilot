"""Read-only candidate overworld telemetry and bounded static-map pathfinding.

Offsets target the supplied Radical Red 4.1 ROM. A successful decode means
structural sanity, not visual validation. Pass validated=True only after live
comparison of map, player movement, NPC locations, and collision with the game.

Sources:
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/BPRE.ld
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/include/global.fieldmap.h
https://github.com/pret/pokefirered/blob/master/src/fieldmap.c

The static ROM map omits script changes and connected-map borders. Re-observe
after each tile, battle, dialogue, or warp; a path is never proof of safe travel.
"""
from __future__ import annotations

from collections import deque
import struct

SAVE_POINTER = 0x03005008
MAP_HEADER = 0x02036DFC
EVENT_OBJECTS = 0x02036E38
PLAYER_AVATAR = 0x02037078
IN_BATTLE = 0x03003529
BATTLE_FLAGS = 0x02022B4C
MAX_CELLS = 16384
MAP_OFFSET = 7


def _region(address, length, *, ram_only=False):
    regions = [(0x02000000, 0x02040000), (0x03000000, 0x03008000)]
    if not ram_only:
        regions.append((0x08000000, 0x0A000000))
    return length > 0 and any(lo <= address and address + length <= hi for lo, hi in regions)


def _read(reader, address, length, *, ram_only=False):
    if not _region(address, length, ram_only=ram_only):
        raise ValueError(f"Invalid read-only pointer/range: {address:#x} + {length}")
    parts = []
    for offset in range(0, length, 1024):
        amount = min(1024, length - offset)
        part = reader(address + offset, amount)
        part = bytes.fromhex(part) if isinstance(part, str) else bytes(part)
        if len(part) != amount:
            raise ValueError("Short memory read")
        parts.append(part)
    return b"".join(parts)


def _u32(data, offset=0):
    return struct.unpack_from("<I", data, offset)[0]


def _point(data, offset=0):
    x, y = struct.unpack_from("<hh", data, offset)
    return {"x": x, "y": y}


def _attributes(reader, tile_ids, primary, secondary):
    attributes = {}
    for tileset, lower, upper in ((primary, 0, 640), (secondary, 640, 1024)):
        ids = sorted(value for value in tile_ids if lower <= value < upper)
        if not ids:
            continue
        table = _u32(_read(reader, tileset, 24), 20)
        first, last = ids[0] - lower, ids[-1] - lower
        raw = _read(reader, table + first * 4, (last - first + 1) * 4)
        for value in ids:
            attributes[value] = _u32(raw, (value - lower - first) * 4)
    return attributes


def _objects(reader, map_group, map_number):
    raw = _read(reader, EVENT_OBJECTS, 16 * 0x24, ram_only=True)
    result = []
    for index in range(16):
        obj = raw[index * 0x24:(index + 1) * 0x24]
        flags = _u32(obj)
        if not flags & 1:
            continue
        position = _point(obj, 16)
        previous = _point(obj, 20)
        for point in (position, previous):
            point["x"] -= MAP_OFFSET
            point["y"] -= MAP_OFFSET
        result.append({"index": index, "local_id": obj[8], "map_group": obj[10],
                       "map_number": obj[9], "position": position, "previous_position": previous,
                       "is_player": bool(flags & (1 << 16)), "invisible": bool(flags & (1 << 13)),
                       "moving": bool(flags & 2), "elevation": obj[11] & 15,
                       "facing": obj[24] & 15, "trainer_type": obj[7],
                       "on_current_map": obj[10] == map_group and obj[9] == map_number})
    return result


def _warps(reader, events_pointer, width, height):
    if not events_pointer:
        return []
    events = _read(reader, events_pointer, 20)
    count = events[1]
    if count > 64:
        raise ValueError("Unreasonable warp count")
    if not count:
        return []
    raw = _read(reader, _u32(events, 8), count * 8)
    warps = []
    for index in range(count):
        record = raw[index * 8:(index + 1) * 8]
        point = _point(record)
        if not (0 <= point["x"] < width and 0 <= point["y"] < height):
            raise ValueError("Warp coordinates outside map")
        warps.append({"index": index, **point, "destination_bytes": list(record[4:8]),
                      "destination_validated": False})
    return warps


def observe_world(read_memory, *, validated=False, include_grid=True):
    """Read one bounded snapshot via read_memory(address, length) -> bytes/hex.

    ``valid`` checks decoded structure, ``validated`` records independent visual
    validation. These have distinct meanings. ``in_battle`` and map fields are
    candidate values while validated is false. No function sends any game input.
    """
    result = {"valid": False, "validated": bool(validated), "source": "read-only game memory",
              "position": None, "map_id": None, "in_battle": None, "grid": None,
              "objects": [], "warps": [], "errors": [], "warnings": []}
    try:
        result["in_battle"] = bool(_read(read_memory, IN_BATTLE, 1)[0] & 2)
        result["battle_flags_candidate"] = _u32(_read(read_memory, BATTLE_FLAGS, 4))
        save = _u32(_read(read_memory, SAVE_POINTER, 4))
        prefix = _read(read_memory, save, 8, ram_only=True)
        position = _point(prefix)
        if not (-7 <= position["x"] <= 255 and -7 <= position["y"] <= 255):
            raise ValueError("Player coordinates outside supported bounds")
        group, number = prefix[4], prefix[5]
        result.update(position=position, map_id=f"{group}.{number}",
                      map_group=group, map_number=number, save_pointer=save)
        header = _read(read_memory, MAP_HEADER, 28, ram_only=True)
        layout_pointer = _u32(header)
        if not layout_pointer:
            raise ValueError("No active map layout; intro/menu may still be running")
        layout = _read(read_memory, layout_pointer, 28)
        width, height = struct.unpack_from("<ii", layout)
        if not (1 <= width <= 256 and 1 <= height <= 256 and width * height <= MAX_CELLS):
            raise ValueError("Map dimensions outside supported bounds")
        result.update(width=width, height=height, layout_id=struct.unpack_from("<H", header, 18)[0],
                      region_id=header[20], map_type=header[23], layout_pointer=layout_pointer)
        result["objects"] = _objects(read_memory, group, number)
        player = next((obj for obj in result["objects"] if obj["is_player"]), None)
        avatar = _read(read_memory, PLAYER_AVATAR, 8, ram_only=True)
        result["avatar_candidate"] = {"flags": avatar[0], "running_state": avatar[2],
                                      "tile_transition_state": avatar[3], "object_index": avatar[5],
                                      "prevent_step": bool(avatar[6])}
        if player:
            result["object_position"] = player["position"]
            result["position_sources_agree"] = player["position"] == position
            result["facing"] = player["facing"]
            if not result["position_sources_agree"]:
                result["warnings"].append("Save position and player object differ; moving or structure unvalidated")
        else:
            result["warnings"].append("No active player object found")
        try:
            result["warps"] = _warps(read_memory, _u32(header, 4), width, height)
        except (ValueError, OSError, TimeoutError) as error:
            result["warnings"].append("Warp decode: " + str(error))
        if include_grid:
            raw = _read(read_memory, _u32(layout, 12), width * height * 2)
            words = struct.unpack("<" + "H" * (width * height), raw)
            attributes = {}
            try:
                attributes = _attributes(read_memory, {word & 1023 for word in words},
                                         _u32(layout, 16), _u32(layout, 20))
            except (ValueError, OSError, TimeoutError) as error:
                result["warnings"].append("Tile attributes: " + str(error))
            grid = {"width": width, "height": height, "validated": bool(validated),
                    "source": "static layout", "walkable": [], "collision": [], "elevation": [],
                    "behavior": [], "terrain": [], "encounter_type": [], "metatile": [],
                    "warp_positions": [{"x": w["x"], "y": w["y"]} for w in result["warps"]],
                    "blocked_positions": [o["position"] for o in result["objects"]
                                          if not o["is_player"] and o["on_current_map"]]}
            for y in range(height):
                rows = {key: [] for key in ("walkable", "collision", "elevation", "behavior", "terrain", "encounter_type", "metatile")}
                for word in words[y * width:(y + 1) * width]:
                    metatile = word & 1023
                    collision, elevation = (word >> 10) & 3, word >> 12
                    attribute = attributes.get(metatile)
                    behavior = attribute & 511 if attribute is not None else None
                    terrain = (attribute >> 9) & 31 if attribute is not None else None
                    encounter = (attribute >> 24) & 7 if attribute is not None else None
                    # Normal foot travel only; ledges/currents/slides need directional mechanics.
                    walkable = (word != 1023 and collision == 0 and terrain in (0, 1)
                                and behavior is not None and not 0x30 <= behavior <= 0x58)
                    for key, value in (("walkable", walkable), ("collision", collision), ("elevation", elevation),
                                       ("behavior", behavior), ("terrain", terrain), ("encounter_type", encounter),
                                       ("metatile", metatile)):
                        rows[key].append(value)
                for key, row in rows.items():
                    grid[key].append(row)
            result["grid"] = grid
        after_save = _u32(_read(read_memory, SAVE_POINTER, 4))
        after_header = _read(read_memory, MAP_HEADER, 4)
        after_prefix = _read(read_memory, after_save, 8, ram_only=True)
        if after_save != save or after_header != header[:4] or after_prefix[4:6] != prefix[4:6]:
            raise ValueError("Map changed during snapshot; observe again")
        result["valid"] = True
    except (ValueError, OSError, TimeoutError, struct.error) as error:
        result["errors"].append(str(error))
    return result


def plan_path(grid, start, target, *, blocked=(), allow_unvalidated=False, avoid_encounters=False):
    """Return shortest cardinal button list, [] at target, or None if unreachable.

    The candidate grid must be visually validated or explicitly opted into.
    Warps are avoided except at the requested target. Dynamic map edits, moving
    NPCs, ledges and trainer sightlines require observation between each step.
    """
    if not grid or (not grid.get("validated") and not allow_unvalidated):
        raise ValueError("A visually validated map grid is required")
    point = lambda value: (int(value["x"]), int(value["y"])) if isinstance(value, dict) else tuple(value)
    start, target = point(start), point(target)
    width, height = grid["width"], grid["height"]
    if not (1 <= width <= 256 and 1 <= height <= 256 and width * height <= MAX_CELLS):
        raise ValueError("Invalid pathfinding dimensions")
    inside = lambda p: 0 <= p[0] < width and 0 <= p[1] < height
    if not inside(start) or not inside(target):
        raise ValueError("Path endpoints are outside the map")
    if start == target:
        return []
    obstacles = {point(p) for p in grid.get("blocked_positions", [])} | {point(p) for p in blocked}
    warps = {point(p) for p in grid.get("warp_positions", [])}
    queue, visited = deque([start]), {start: None}
    while queue:
        current = queue.popleft()
        for dx, dy, button in ((0, -1, "UP"), (1, 0, "RIGHT"), (0, 1, "DOWN"), (-1, 0, "LEFT")):
            nxt = current[0] + dx, current[1] + dy
            if not inside(nxt) or nxt in visited or nxt in obstacles or (nxt in warps and nxt != target):
                continue
            x, y = nxt
            if not grid["walkable"][y][x]:
                continue
            if avoid_encounters and grid.get("encounter_type", [])[y][x] != 0:
                continue
            elevation = grid.get("elevation")
            if elevation:
                a, b = elevation[current[1]][current[0]], elevation[y][x]
                if a and b and a != b:
                    continue
            visited[nxt] = (current, button)
            if nxt == target:
                result = []
                while visited[nxt] is not None:
                    nxt, action = visited[nxt]
                    result.append(action)
                return result[::-1]
            queue.append(nxt)
    return None
