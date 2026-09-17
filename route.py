"""Cross-map routing from links the game itself stores.

`navigation.py` walks one map. This layer answers the question it cannot: which
tile to walk to in order to reach a *different* map, and how to cross the link
once standing on it. Both link kinds are read out of the live map header, so the
world is discovered rather than hardcoded:

    warps        indoor doors and stairs; destination map is in the warp record
    connections  outdoor map edges; direction and destination are in the header

A leg is therefore: pick the link to the next map, hand its approach tile to the
navigator, then press the crossing direction until the map id actually changes.
Warps come in two shapes and both are handled: a doormat or staircase sits on a
walkable tile you stand on and step off, while a building door is set into a wall
you never stand on and must walk into from its one walkable neighbour.
Nothing here reads pixels and nothing guesses a destination it has not read.

Sources:
https://github.com/pret/pokefirered/blob/master/include/global.fieldmap.h
https://github.com/pret/pokefirered/blob/master/src/field_screen_effect.c
"""

from __future__ import annotations

import struct

MAP_HEADER = 0x02036DFC
CONNECTIONS_OFFSET = 0x0C
MAX_CONNECTIONS = 16
# global.fieldmap.h: CONNECTION_SOUTH=1, NORTH=2, WEST=3, EAST=4, DIVE=5, EMERGE=6.
CONNECTION_BUTTONS = {1: 'DOWN', 2: 'UP', 3: 'LEFT', 4: 'RIGHT'}
_STEPS = {'UP': (0, -1), 'DOWN': (0, 1), 'LEFT': (-1, 0), 'RIGHT': (1, 0)}
CROSSING_FRAMES = 24
MAX_CROSSING_PRESSES = 10
MAX_WALK_ATTEMPTS = 8
TRIES_PER_DIRECTION = 2


def map_id(group: int, number: int) -> str:
    if not 0 <= group <= 255 or not 0 <= number <= 255:
        raise ValueError('Map group and number must be single bytes.')
    return '%d.%d' % (group, number)


def warp_destination(warp: dict) -> str:
    """Decode a warp record's destination map.

    The four trailing bytes of a FireRed warp event are elevation, warp id,
    map number and map group, in that order.
    """
    raw = warp.get('destination_bytes')
    if not isinstance(raw, list) or len(raw) != 4 or any(
            not isinstance(value, int) or not 0 <= value <= 255 for value in raw):
        raise ValueError('A warp needs four destination bytes.')
    return map_id(raw[3], raw[2])


def read_connections(read_memory) -> list[dict]:
    """Read the current map's edge connections. Returns [] when a map has none."""
    header = read_memory(MAP_HEADER, 28)
    pointer = struct.unpack_from('<I', header, CONNECTIONS_OFFSET)[0]
    if not pointer:
        return []
    block = read_memory(pointer, 8)
    count, array = struct.unpack_from('<II', block, 0)
    if count > MAX_CONNECTIONS:
        raise ValueError('Unreasonable connection count')
    if not count or not array:
        return []
    raw = read_memory(array, count * 12)
    links = []
    for index in range(count):
        direction, offset, group, number = struct.unpack_from('<iiBB', raw, index * 12)
        if direction not in CONNECTION_BUTTONS:
            continue  # Dive and emerge are not foot travel.
        links.append({'direction': direction, 'button': CONNECTION_BUTTONS[direction],
                      'offset': offset, 'map_id': map_id(group, number)})
    return links


def plan_map_route(graph: dict, start: str, target: str) -> list[str] | None:
    """Shortest map-to-map hop list including both ends, or None if unreachable."""
    if start == target:
        return [start]
    seen, queue = {start}, [[start]]
    while queue:
        path = queue.pop(0)
        for neighbour in graph.get(path[-1], ()):  # insertion order is read order
            if neighbour in seen:
                continue
            if neighbour == target:
                return path + [neighbour]
            seen.add(neighbour)
            queue.append(path + [neighbour])
    return None


def crossing_buttons(grid: dict, tile: tuple[int, int], approach: str | None = None) -> list[str]:
    """Directions that might cross a link, best first.

    A link tile is left by walking into something not walkable: a doormat steps
    off the map edge, a staircase descends into the stair graphic. Both look the
    same from the collision grid, and a bedroom staircase genuinely had two
    candidates — UP into the wall and LEFT down the stairs, where only LEFT
    worked. So every candidate is returned and the caller tries them in turn
    rather than committing to a guess. The approach direction leads because a
    doormat is crossed by continuing the way you walked in.
    """
    width, height = grid.get('width'), grid.get('height')
    walkable = grid.get('walkable')
    if not isinstance(width, int) or not isinstance(height, int) or not isinstance(walkable, list):
        raise ValueError('A validated collision grid is required.')
    x, y = tile
    options = []
    for button, (dx, dy) in _STEPS.items():
        nx, ny = x + dx, y + dy
        if not (0 <= nx < width and 0 <= ny < height) or not walkable[ny][nx]:
            options.append(button)
    if approach in options:
        options.remove(approach)
        options.insert(0, approach)
    return options


def crossing_button(grid: dict, tile: tuple[int, int], approach: str | None = None) -> str | None:
    """First crossing candidate, or None when a tile has no off-map side."""
    options = crossing_buttons(grid, tile, approach)
    return options[0] if options else None


def warp_approach(grid: dict, tile: tuple[int, int]) -> dict | None:
    """Where to stand to use a warp, and which way to press.

    A walkable warp (doormat, staircase) is stood on and stepped off the map. A
    warp set into a wall is never stood on: approach it from its one walkable
    neighbour and walk into it. Returns None when neither shape applies.
    """
    width, height = grid.get('width'), grid.get('height')
    walkable = grid.get('walkable')
    if not isinstance(width, int) or not isinstance(height, int) or not isinstance(walkable, list):
        raise ValueError('A validated collision grid is required.')
    x, y = tile
    if not (0 <= x < width and 0 <= y < height):
        raise ValueError('Warp tile lies outside the map.')
    if walkable[y][x]:
        options = crossing_buttons(grid, tile)
        return {'tile': (x, y), 'button': options[0], 'alternatives': options} if options else None
    for button, (dx, dy) in _STEPS.items():
        nx, ny = x - dx, y - dy
        if 0 <= nx < width and 0 <= ny < height and walkable[ny][nx]:
            return {'tile': (nx, ny), 'button': button, 'alternatives': [button]}
    return None


class RouteSession:
    """One itinerary of map hops. Construct a new session per destination."""

    def __init__(self, itinerary: list[str], *, final_tile: tuple[int, int] | None = None):
        if not itinerary or any(not isinstance(item, str) for item in itinerary):
            raise ValueError('An itinerary needs at least one map id.')
        if len(set(itinerary)) != len(itinerary):
            raise ValueError('An itinerary must not repeat a map.')
        self.itinerary = list(itinerary)
        self.final_tile = tuple(final_tile) if final_tile else None
        self.crossing_presses = 0
        self._crossing_from: str | None = None
        self.walk_attempts = 0
        self._walk_key: tuple | None = None
        # A door drawn two tiles wide lists both tiles as warps, but only one of
        # them actually triggers. A tile that will not cross is recorded so the
        # other candidates get their turn instead of the route stalling.
        self.failed_links: set = set()

    def _stop(self, status, reason, **extra):
        return dict(status=status, reason=reason, **extra)

    def choose(self, world: dict, connections: list[dict]) -> dict:
        """Decide the next move toward the itinerary's end.

        Returns a status of `arrived`, `walk` (a tile goal for the navigator),
        `cross` (a button to press now) or `paused` with an honest reason.
        """
        if not world.get('validated') or not world.get('valid'):
            return self._stop('paused', 'A validated overworld is required before routing.')
        if world.get('in_battle'):
            return self._stop('paused', 'A battle is in progress; routing yields to the battle policy.')
        current = world.get('map_id')
        if current not in self.itinerary:
            return self._stop('paused', 'Map %s is not on the planned route; re-plan from here.' % current)
        index = self.itinerary.index(current)
        if current != self._crossing_from:
            self.crossing_presses, self._crossing_from = 0, current
        if index == len(self.itinerary) - 1:
            if self.final_tile is None:
                return self._stop('arrived', 'Reached the final map %s on the route.' % current)
            position = world.get('position') or {}
            if (position.get('x'), position.get('y')) == self.final_tile:
                return self._stop('arrived', 'Reached (%d, %d) on %s.' % (*self.final_tile, current))
            return self._stop('walk', 'Walking to the route destination on %s.' % current,
                              target={'x': self.final_tile[0], 'y': self.final_tile[1]}, allow_warp=False)
        nxt = self.itinerary[index + 1]
        grid = world.get('grid')
        if not isinstance(grid, dict) or grid.get('validated') is not True:
            return self._stop('paused', 'A validated collision grid is required to reach the next map.')
        position = world.get('position') or {}
        here = (position.get('x'), position.get('y'))

        for link in connections:
            if link['map_id'] != nxt:
                continue
            button = link['button']
            edge = self._edge_tile(grid, button, here)
            if edge is None:
                return self._stop('paused', 'No walkable tile on the %s edge of %s.' % (button, current))
            if here != edge:
                return self._walk('Walking to the %s edge of %s toward %s.' % (button, current, nxt),
                                  current, here, edge, False)
            return self._crossing([button], 'Crossing the %s edge of %s into %s.' % (button, current, nxt))

        for warp in world.get('warps') or []:
            if warp_destination(warp) != nxt:
                continue
            tile = (warp['x'], warp['y'])
            if (current, tile) in self.failed_links:
                continue
            approach = warp_approach(grid, tile)
            if approach is None:
                return self._stop('paused', 'The exit at (%d, %d) on %s cannot be reached on foot.'
                                  % (tile[0], tile[1], current))
            if here != approach['tile']:
                return self._walk('Walking to the %s exit on %s toward %s.' % (nxt, current, nxt),
                                  current, here, approach['tile'], approach['tile'] == tile)
            return self._crossing(approach.get('alternatives') or [approach['button']],
                                  'Stepping through the %s exit on %s into %s.' % (nxt, current, nxt),
                                  link=(current, tile))

        return self._stop('paused', 'No warp or connection on %s leads to %s; the route is wrong here.'
                          % (current, nxt))

    def _walk(self, reason, current, here, target, allow_warp):
        """Hand one tile goal to the navigator, refusing to reissue a goal it cannot reach."""
        key = (current, here, tuple(target))
        self.walk_attempts = self.walk_attempts + 1 if key == self._walk_key else 1
        self._walk_key = key
        if self.walk_attempts > MAX_WALK_ATTEMPTS:
            return self._stop('paused', 'The navigator could not reach (%d, %d) on %s from (%s, %s) after '
                              '%d attempts; no walking route exists.'
                              % (target[0], target[1], current, here[0], here[1], self.walk_attempts - 1))
        return self._stop('walk', reason, target={'x': target[0], 'y': target[1]}, allow_warp=allow_warp)

    def _crossing(self, options, reason, *, link=None):
        """Try each candidate direction in turn rather than repeating a guess."""
        self.crossing_presses += 1
        attempt = self.crossing_presses - 1
        budget = TRIES_PER_DIRECTION * len(options)
        if attempt >= budget or self.crossing_presses > MAX_CROSSING_PRESSES:
            if link is not None and link not in self.failed_links:
                self.failed_links.add(link)
                self.crossing_presses = 0
                return self._stop('retry', 'The exit at (%d, %d) did not open with %s; trying another '
                                  'tile of the same doorway.' % (link[1][0], link[1][1], ' or '.join(options)))
            return self._stop('paused', 'Tried %s on the link tile without the map changing.'
                              % ' and '.join(options))
        button = options[attempt // TRIES_PER_DIRECTION]
        if attempt >= TRIES_PER_DIRECTION:
            reason += ' %s did not work; trying %s.' % (options[0], button)
        return self._stop('cross', reason, button=button, frames=CROSSING_FRAMES)

    @staticmethod
    def _edge_tile(grid, button, here):
        """Walkable tile on the named edge, nearest the player's current lane."""
        width, height, walkable = grid['width'], grid['height'], grid['walkable']
        if button in ('UP', 'DOWN'):
            row = 0 if button == 'UP' else height - 1
            lane = here[0] if here[0] is not None else width // 2
            options = [(x, row) for x in range(width) if walkable[row][x]]
        else:
            column = 0 if button == 'LEFT' else width - 1
            lane = here[1] if here[1] is not None else height // 2
            options = [(column, y) for y in range(height) if walkable[y][column]]
        if not options:
            return None
        key = (lambda t: abs(t[0] - lane)) if button in ('UP', 'DOWN') else (lambda t: abs(t[1] - lane))
        return min(options, key=key)
