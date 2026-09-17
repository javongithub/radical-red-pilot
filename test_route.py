"""Isolated routing tests; no ROM, model, or emulator needed."""

import struct
import unittest

import route
from route import (RouteSession, crossing_button, crossing_buttons, plan_map_route,
                   read_connections, warp_approach, warp_destination)


def grid(width=6, height=5, blocked=()):
    walkable = [[(x, y) not in blocked for x in range(width)] for y in range(height)]
    return {'width': width, 'height': height, 'validated': True, 'walkable': walkable}


def world(map_id='3.0', position=(3, 2), warps=(), **kwargs):
    base = {'valid': True, 'validated': True, 'in_battle': False, 'map_id': map_id,
            'position': {'x': position[0], 'y': position[1]}, 'grid': grid(), 'warps': list(warps)}
    base.update(kwargs)
    return base


def warp(x, y, group, number):
    return {'index': 0, 'x': x, 'y': y, 'destination_bytes': [3, 0, number, group]}


class DecodeTests(unittest.TestCase):
    def test_warp_destination_reads_group_and_number(self):
        # The live house door observed on map 4.0 carried [3, 0, 0, 3] -> Pallet Town.
        self.assertEqual(warp_destination({'destination_bytes': [3, 0, 0, 3]}), '3.0')
        self.assertEqual(warp_destination({'destination_bytes': [3, 2, 1, 4]}), '4.1')

    def test_malformed_warps_are_rejected(self):
        for bad in ({}, {'destination_bytes': [1, 2, 3]}, {'destination_bytes': [1, 2, 3, 300]},
                    {'destination_bytes': 'abcd'}):
            with self.assertRaises(ValueError):
                warp_destination(bad)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        # Mirrors the live Pallet Town read: north to 3.19, south to 3.39.
        entries = struct.pack('<iiBBH', 2, 0, 3, 19, 0) + struct.pack('<iiBBH', 1, 0, 3, 39, 0)
        self.memory = {0x02036DFC: struct.pack('<IIII', 0, 0, 0, 0x08350000) + bytes(12),
                       0x08350000: struct.pack('<II', 2, 0x08350100), 0x08350100: entries}

    def read(self, address, length):
        return self.memory[address][:length]

    def test_connections_decode_direction_and_destination(self):
        links = read_connections(self.read)
        self.assertEqual([(l['button'], l['map_id']) for l in links],
                         [('UP', '3.19'), ('DOWN', '3.39')])

    def test_map_without_connections_returns_empty(self):
        self.memory[0x02036DFC] = bytes(28)
        self.assertEqual(read_connections(self.read), [])

    def test_dive_and_emerge_are_not_foot_travel(self):
        self.memory[0x08350100] = struct.pack('<iiBBH', 5, 0, 3, 19, 0) + struct.pack('<iiBBH', 2, 0, 3, 20, 0)
        self.assertEqual([l['map_id'] for l in read_connections(self.read)], ['3.20'])

    def test_absurd_connection_count_is_rejected(self):
        self.memory[0x08350000] = struct.pack('<II', 999, 0x08350100)
        with self.assertRaises(ValueError):
            read_connections(self.read)


class GraphTests(unittest.TestCase):
    graph = {'3.0': ['3.19'], '3.19': ['3.0', '3.1'], '3.1': ['3.19', '3.20'], '3.20': ['3.1', '3.2'], '3.2': ['3.20']}

    def test_route_to_pewter_is_the_kanto_chain(self):
        self.assertEqual(plan_map_route(self.graph, '3.0', '3.2'),
                         ['3.0', '3.19', '3.1', '3.20', '3.2'])

    def test_same_map_is_a_single_hop(self):
        self.assertEqual(plan_map_route(self.graph, '3.0', '3.0'), ['3.0'])

    def test_unreachable_map_returns_none(self):
        self.assertIsNone(plan_map_route(self.graph, '3.0', '9.9'))


class CrossingTests(unittest.TestCase):
    def test_door_on_the_south_wall_is_crossed_downward(self):
        # A doormat on the bottom row: the only off-map side is south.
        self.assertEqual(crossing_button(grid(height=5), (3, 4)), 'DOWN')

    def test_approach_direction_breaks_a_corner_tie(self):
        self.assertEqual(crossing_button(grid(), (0, 4), approach='DOWN'), 'DOWN')
        self.assertEqual(crossing_button(grid(), (0, 4), approach='LEFT'), 'LEFT')

    def test_open_tile_has_no_crossing(self):
        self.assertIsNone(crossing_button(grid(), (3, 2)))


class ApproachTests(unittest.TestCase):
    def test_doormat_is_stood_on_and_stepped_off(self):
        # A walkable warp on the bottom row: stand on it, then press off-map.
        found = warp_approach(grid(height=5), (3, 4))
        self.assertEqual((found['tile'], found['button']), ((3, 4), 'DOWN'))

    def test_door_set_into_a_wall_is_entered_from_below(self):
        # Oak's lab door as actually read from Pallet Town: ###L### with open ground beneath.
        walls = {(x, y) for x in range(13, 20) for y in (12, 13)} - set()
        board = grid(width=24, height=20, blocked=walls)
        found = warp_approach(board, (16, 13))
        self.assertEqual((found['tile'], found['button']), ((16, 14), 'UP'))

    def test_a_sealed_warp_has_no_approach(self):
        sealed = grid(width=5, height=5, blocked={(x, y) for x in range(5) for y in range(5)})
        self.assertIsNone(warp_approach(sealed, (2, 2)))

    def test_warp_outside_the_map_is_rejected(self):
        with self.assertRaises(ValueError):
            warp_approach(grid(), (99, 99))


class SessionTests(unittest.TestCase):
    def test_walks_to_the_connection_edge_then_crosses(self):
        session = RouteSession(['3.0', '3.19'])
        links = [{'button': 'UP', 'map_id': '3.19', 'direction': 2, 'offset': 0}]
        first = session.choose(world(position=(3, 2)), links)
        self.assertEqual(first['status'], 'walk')
        self.assertEqual(first['target'], {'x': 3, 'y': 0})
        second = session.choose(world(position=(3, 0)), links)
        self.assertEqual(second['status'], 'cross')
        self.assertEqual(second['button'], 'UP')

    def test_walks_to_a_warp_then_steps_through(self):
        session = RouteSession(['4.0', '3.0'])
        door = warp(2, 4, 3, 0)
        first = session.choose(world(map_id='4.0', position=(3, 2), warps=[door]), [])
        self.assertEqual(first['status'], 'walk')
        self.assertEqual((first['target'], first['allow_warp']), ({'x': 2, 'y': 4}, True))
        # A walkable doormat is the approach tile itself, so warping onto it is allowed.
        second = session.choose(world(map_id='4.0', position=(2, 4), warps=[door]), [])
        self.assertEqual((second['status'], second['button']), ('cross', 'DOWN'))

    def test_arrival_is_reported_once_the_last_map_is_reached(self):
        session = RouteSession(['3.0', '3.19'])
        self.assertEqual(session.choose(world(map_id='3.19'), [])['status'], 'arrived')

    def test_final_tile_is_walked_to_before_arrival(self):
        session = RouteSession(['3.0'], final_tile=(1, 1))
        self.assertEqual(session.choose(world(position=(3, 2)), [])['status'], 'walk')
        self.assertEqual(session.choose(world(position=(1, 1)), [])['status'], 'arrived')

    def test_a_link_that_does_not_exist_pauses_honestly(self):
        session = RouteSession(['3.0', '3.19'])
        outcome = session.choose(world(), [])
        self.assertEqual(outcome['status'], 'paused')
        self.assertIn('leads to 3.19', outcome['reason'])

    def test_repeated_crossing_without_a_map_change_pauses(self):
        session = RouteSession(['3.0', '3.19'])
        links = [{'button': 'UP', 'map_id': '3.19', 'direction': 2, 'offset': 0}]
        seen = [session.choose(world(position=(3, 0)), links)['status'] for _ in range(8)]
        self.assertIn('cross', seen)
        self.assertEqual(seen[-1], 'paused')

    def test_a_battle_yields_instead_of_routing(self):
        session = RouteSession(['3.0', '3.19'])
        outcome = session.choose(world(in_battle=True), [])
        self.assertEqual(outcome['status'], 'paused')
        self.assertIn('battle', outcome['reason'])

    def test_unvalidated_world_never_routes(self):
        session = RouteSession(['3.0', '3.19'])
        self.assertEqual(session.choose(world(validated=False), [])['status'], 'paused')

    def test_leaving_the_planned_route_pauses(self):
        session = RouteSession(['3.0', '3.19'])
        self.assertEqual(session.choose(world(map_id='7.7'), [])['status'], 'paused')

    def test_malformed_itineraries_are_rejected(self):
        for bad in ([], ['3.0', '3.0'], [1, 2]):
            with self.assertRaises(ValueError):
                RouteSession(bad)


if __name__ == '__main__':
    unittest.main()


class WalkGuardTests(unittest.TestCase):
    def test_an_unreachable_goal_pauses_instead_of_looping(self):
        session = RouteSession(['3.0', '3.19'])
        links = [{'button': 'UP', 'map_id': '3.19', 'direction': 2, 'offset': 0}]
        # The navigator keeps failing, so the player never leaves this tile.
        seen = [session.choose(world(position=(3, 2)), links)['status'] for _ in range(12)]
        self.assertEqual(seen[0], 'walk')
        self.assertEqual(seen[-1], 'paused')

    def test_progress_resets_the_walk_guard(self):
        session = RouteSession(['3.0', '3.19'])
        links = [{'button': 'UP', 'map_id': '3.19', 'direction': 2, 'offset': 0}]
        for y in (4, 3, 2, 1):
            outcome = session.choose(world(position=(3, y)), links)
            self.assertEqual(outcome['status'], 'walk')


class StairsTests(unittest.TestCase):
    """A bedroom staircase: two unwalkable neighbours, only one of them crosses."""

    def stairs(self):
        # Rows 0-1 are wall, and (9,2) is the staircase graphic beside the tile.
        walkable = [[not (y < 2 or x == 0 or (x == 9 and y == 2)) for x in range(12)]
                    for y in range(9)]
        return {'width': 12, 'height': 9, 'validated': True, 'walkable': walkable}

    def test_both_candidates_are_offered(self):
        self.assertEqual(crossing_buttons(self.stairs(), (10, 2)), ['UP', 'LEFT'])

    def test_the_approach_direction_leads(self):
        self.assertEqual(crossing_buttons(self.stairs(), (10, 2), approach='LEFT')[0], 'LEFT')

    def test_a_second_direction_is_tried_after_the_first_fails(self):
        session = RouteSession(['4.1', '4.0'])
        door = {'index': 0, 'x': 10, 'y': 2, 'destination_bytes': [3, 2, 0, 4]}
        world = {'valid': True, 'validated': True, 'in_battle': False, 'map_id': '4.1',
                 'position': {'x': 10, 'y': 2}, 'grid': self.stairs(), 'warps': [door]}
        pressed = []
        for _ in range(5):
            outcome = session.choose(world, [])
            if outcome['status'] != 'cross':
                break
            pressed.append(outcome['button'])
        # UP is tried first and, when the map never changes, LEFT gets its turn.
        self.assertEqual(pressed[0], 'UP')
        self.assertIn('LEFT', pressed)

    def test_it_still_gives_up_honestly(self):
        session = RouteSession(['4.1', '4.0'])
        door = {'index': 0, 'x': 10, 'y': 2, 'destination_bytes': [3, 2, 0, 4]}
        world = {'valid': True, 'validated': True, 'in_battle': False, 'map_id': '4.1',
                 'position': {'x': 10, 'y': 2}, 'grid': self.stairs(), 'warps': [door]}
        last = None
        for _ in range(12):
            last = session.choose(world, [])
        self.assertEqual(last['status'], 'paused')


class TwoTileDoorTests(unittest.TestCase):
    """A doorway drawn two tiles wide lists both tiles, but only one triggers."""

    def world_with_two_doors(self, position):
        walkable = [[not (y == 9 or x == 0) for x in range(13)] for y in range(10)]
        grid = {'width': 13, 'height': 10, 'validated': True, 'walkable': walkable}
        doors = [{'index': 0, 'x': 5, 'y': 8, 'destination_bytes': [3, 0, 0, 3]},
                 {'index': 1, 'x': 4, 'y': 8, 'destination_bytes': [3, 0, 0, 3]}]
        return {'valid': True, 'validated': True, 'in_battle': False, 'map_id': '4.0',
                'position': {'x': position[0], 'y': position[1]}, 'grid': grid, 'warps': doors}

    def test_a_dead_door_tile_is_abandoned_for_its_neighbour(self):
        session = RouteSession(['4.0', '3.0'])
        targets, statuses = [], []
        for _ in range(12):
            outcome = session.choose(self.world_with_two_doors((5, 8)), [])
            statuses.append(outcome['status'])
            if outcome['status'] == 'walk':
                targets.append((outcome['target']['x'], outcome['target']['y']))
        self.assertIn('retry', statuses)
        # Having given up on (5,8) it must now walk to the other door tile.
        self.assertIn((4, 8), targets)

    def test_it_still_pauses_once_every_tile_is_exhausted(self):
        session = RouteSession(['4.0', '3.0'])
        last = None
        for _ in range(40):
            last = session.choose(self.world_with_two_doors((5, 8)), [])
        self.assertEqual(last['status'], 'paused')
