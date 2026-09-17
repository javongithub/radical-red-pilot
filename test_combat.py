"""Battle decoder checks using independent CFRU-shaped memory fixtures.

Run: python3 -m unittest -v test_combat.py

The offsets are specified by CFRU include/pokemon.h, include/main.h and
include/constants/battle.h; symbols are from BPRE.ld. These tests do not run
the emulator or imply that a live Radical Red battle has been validated.
"""
import struct
import unittest
from unittest.mock import patch

import combat

ACTIVE = 0x03003529
COUNT = 0x02023BCC
POSITIONS = 0x02023BD6
MONS = 0x02023BE4
FLAGS = 0x02022B4C


def battle_mon(species=1, *, hp=21, maximum=30, level=7, moves=(1, 0, 2, 0)):
    # Struct has u32 otId at0x54, so its aligned size is0x58 (88).
    b = bytearray(0x58)
    struct.pack_into('<6H', b, 0, species, 11, 12, 13, 14, 15)
    struct.pack_into('<4H', b, 0x0C, *moves)
    b[0x19:0x20] = bytes((6, 7, 8, 5, 4, 9, 3))
    b[0x24:0x28] = bytes((0, 255, 17, 255))
    struct.pack_into('<H', b, 0x28, hp)
    b[0x2A] = level
    struct.pack_into('<H', b, 0x2C, maximum)
    b[0x30:0x3B] = bytes((0xBB, 0xBC, 0xFF, 1, 2, 3, 4, 5, 6, 7, 8))
    struct.pack_into('<I', b, 0x54, 0xDEADBEEF)
    return bytes(b)


class Memory:
    def __init__(self, *, records=None, positions=None, flags=8, active=2, count=None):
        records = records or [battle_mon(), battle_mon(2, hp=17, maximum=40, moves=(65000, 65001, 65002, 65003))]
        positions = positions if positions is not None else list(range(len(records)))
        self.data = {ACTIVE: bytes((active,)), COUNT: bytes((len(records) if count is None else count,)),
                     POSITIONS: bytes(positions), MONS: b''.join(records), FLAGS: struct.pack('<I', flags)}
        self.calls = []
        self.after_read = None

    def __call__(self, address, length):
        self.calls.append((address, length))
        if address not in self.data:
            raise AssertionError(f'Unexpected read of private or unknown data: {address:#x}')
        if not 0 < length <= 1024:
            raise AssertionError('Bridge read budget exceeded')
        result = self.data[address][:length]
        if self.after_read:
            self.after_read(self, address)
        return result


class CombatTests(unittest.TestCase):
    def setUp(self):
        # Real table parser used; only the fingerprinted-ROM loading boundary is mocked.
        rom = bytearray(0x500)
        species_base, move_base = 0x100, 0x300
        for i, types in ((1, (12, 3)), (2, (10, 10)), (3, (23, 14)), (4, (11, 15))):
            rom[species_base+28*i:species_base+28*i+9] = bytes((45, 49, 49, 45, 65, 65, *types, 45))
        rom[move_base+12:move_base+24] = bytes((0, 40, 0, 100, 35, 0, 0, 0, 0, 0, 0, 0))
        rom[move_base+24:move_base+36] = bytes((9, 0, 14, 100, 20, 0, 0, 255, 0, 0, 2, 0))
        self.mock_tables = patch.object(combat, 'tables', return_value=(
            bytes(rom), species_base, move_base,
            ['', 'Bulbasaur', 'Charmander', 'FairyFixture', 'IceFixture'],
            ['', 'Pound', 'StatusFixture']))
        self.mock_tables.start()
        self.addCleanup(self.mock_tables.stop)

    def observe(self, memory, **kwargs):
        return combat.observe_battle(memory, 'synthetic.gba', **kwargs)

    def assert_invalid(self, memory):
        result = self.observe(memory, validated=True)
        self.assertIs(result['valid'], False)
        self.assertIs(result['validated'], False)
        self.assertNotIn('combatants', result)

    def test_active_bit_only_and_no_stale_battle_reads_outside_battle(self):
        for active in (0, 1, 4, 5, 0xFD):
            with self.subTest(active=active):
                memory = Memory(active=active)
                self.assertEqual(self.observe(memory), {'valid': True, 'validated': False, 'in_battle': False})
                self.assertEqual(memory.calls, [(ACTIVE, 1)])

    def test_trainer_bit_does_not_confuse_master_or_tutorial_flags(self):
        for flags, expected in ((0, False), (4, False), (8, True), (9, True), (12, True), (16, False), (0x400008, True)):
            with self.subTest(flags=flags):
                self.assertIs(self.observe(Memory(flags=flags))['trainer_battle'], expected)

    def test_layout_and_sparse_own_moves_preserve_slots_and_zero_pp(self):
        own = self.observe(Memory())['combatants'][0]
        self.assertEqual((own['hp'], own['max_hp'], own['level'], own['nickname']), (21, 30, 7, 'AB'))
        self.assertEqual(own['stats'], dict(attack=11, defense=12, speed=13, sp_attack=14, sp_defense=15))
        self.assertEqual(own['stat_stages'], [6, 7, 8, 5, 4, 9, 3])
        self.assertEqual([(m['id'], m['slot'], m['pp']) for m in own['moves']], [(1, 1, 0), (2, 3, 17)])
        self.assertEqual(own['moves'][1]['priority'], -1)
        self.assertEqual(own['moves'][1]['category'], 'status')

    def test_double_battle_stride_and_sides_follow_positions_not_array_index(self):
        records = [battle_mon(i, hp=10+i, moves=(1, 0, 0, 0)) for i in (1, 2, 3, 4)]
        memory = Memory(records=records, positions=(3, 0, 1, 2))
        mons = self.observe(memory)['combatants']
        self.assertEqual([m['species_id'] for m in mons], [1, 2, 3, 4])
        self.assertEqual([m['side'] for m in mons], ['opponent', 'player', 'opponent', 'player'])
        self.assertEqual([m.get('hp') for m in mons], [None, 12, None, 14])
        self.assertIn((MONS, 352), memory.calls)

    def test_opponent_move_ids_and_private_fields_are_not_exposed(self):
        enemy = self.observe(Memory())['combatants'][1]
        self.assertEqual(set(enemy), {'position', 'side', 'species_id', 'species', 'types', 'level', 'hp_bar_fraction'})
        self.assertEqual(enemy['types'], ['Fire'])
        self.assertTrue(0 < enemy['hp_bar_fraction'] < 1)
        # Its deliberately invalid hidden move IDs must not be looked up.

    def test_validation_is_never_inferred_from_plausible_values(self):
        self.assertIs(self.observe(Memory())['validated'], False)
        self.assertIs(self.observe(Memory(), validated=True)['validated'], True)

    def test_fainted_own_mon_remains_visible_at_zero_hp(self):
        result = self.observe(Memory(records=[battle_mon(hp=0), battle_mon(2)]))
        self.assertTrue(result['valid'])
        self.assertEqual(result['combatants'][0]['hp'], 0)

    def test_transition_counts_stop_before_decoding(self):
        for count in (0, 1, 3, 5, 255):
            with self.subTest(count=count):
                memory = Memory(count=count)
                self.assert_invalid(memory)
                self.assertNotIn(MONS, [address for address, _ in memory.calls])

    def test_impossible_level_or_hp_rejected(self):
        for params in (dict(level=0), dict(level=101), dict(hp=31), dict(maximum=0, hp=0), dict(maximum=1000)):
            with self.subTest(params=params):
                self.assert_invalid(Memory(records=[battle_mon(**params), battle_mon(2)]))

    def test_invalid_or_duplicate_positions_rejected(self):
        for positions in ((0, 0), (0, 4), (254, 255)):
            with self.subTest(positions=positions):
                self.assert_invalid(Memory(positions=positions))

    def test_short_reads_return_invalid_instead_of_usable_partial_facts(self):
        for address in (ACTIVE, COUNT, POSITIONS, MONS, FLAGS):
            with self.subTest(address=hex(address)):
                memory = Memory()
                memory.data[address] = memory.data[address][:-1]
                self.assert_invalid(memory)

    def test_unknown_species_returns_invalid(self):
        self.assert_invalid(Memory(records=[battle_mon(65000), battle_mon(2)]))

    def test_battle_ending_during_snapshot_cannot_return_valid(self):
        memory = Memory()
        memory.after_read = lambda m, address: m.data.__setitem__(ACTIVE, b'\x00') if address == MONS else None
        self.assert_invalid(memory)

    def test_battler_count_changing_during_snapshot_cannot_return_valid(self):
        memory = Memory()
        memory.after_read = lambda m, address: m.data.__setitem__(COUNT, b'\x04') if address == MONS else None
        self.assert_invalid(memory)

    def test_battler_positions_changing_during_snapshot_cannot_return_valid(self):
        memory = Memory()
        memory.after_read = lambda m, address: m.data.__setitem__(POSITIONS, b'\x01\x00') if address == MONS else None
        self.assert_invalid(memory)


if __name__ == '__main__':
    unittest.main()
