"""Player move labels read from this exact Radical Red 4.1 ROM.

This module reads no emulator/enemy memory. It only enriches the player's already
observed move IDs and PP with static names from the supplied ROM. Live party
validation remains the caller's responsibility. Species types, move power,
abilities, and learnsets are deliberately omitted: their tables are not validated.

Evidence in the fingerprinted ROM:
  Full names: 0x010EEEDC, 1004 entries of 17 bytes, ending at 0x010F3188.
  Battle labels: 0x010F3188, 13-byte entries. Multiple engine pointers reference
  both tables; paired pointers at 0x01071454/0x01071458 point to short/full names.
  Entries 1/2/3: Pound, Karate Chop, Double Slap. Entry 1003: Rapid Flow.
  Header 0x148 is NOT the move-name table in this ROM.

The original Gen 3 move IDs are also documented in the CFRU primary source:
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/include/constants/moves.h
Expansion IDs here come from this ROM itself, not a different game's numbering.
"""

from functools import lru_cache
import hashlib
from pathlib import Path
import struct

from telemetry import ROM_SHA256, decode_text

FULL_NAMES = 0x010EEEDC
SHORT_NAMES = 0x010F3188
MOVE_COUNT = 1004
FULL_WIDTH = 17
SHORT_WIDTH = 13
CHECKS = {
    0: '-', 1: 'Pound', 2: 'Karate Chop', 3: 'Double Slap', 10: 'Scratch',
    33: 'Tackle', 39: 'Tail Whip', 45: 'Growl', 84: 'Thunder Shock',
    85: 'Thunderbolt', 98: 'Quick Attack', 165: 'Struggle',
    355: 'Leech Fang', 719: 'Grassy Glide', 800: 'Chilly Reception',
    1003: 'Rapid Flow',
}


@lru_cache(maxsize=2)
def _load_names(path, modified_ns, size):
    rom = Path(path).read_bytes()
    if hashlib.sha256(rom).hexdigest() != ROM_SHA256:
        raise ValueError('Unsupported ROM fingerprint; move labels must be revalidated.')
    if struct.unpack_from('<II', rom, 0x01071454) != (SHORT_NAMES + 0x08000000, FULL_NAMES + 0x08000000):
        raise ValueError('Move-name pointer checks failed.')
    if FULL_NAMES + FULL_WIDTH * MOVE_COUNT != SHORT_NAMES:
        raise ValueError('Move-name table boundary check failed.')
    names, short_names = [], []
    for move_id in range(MOVE_COUNT):
        full = rom[FULL_NAMES + move_id * FULL_WIDTH:FULL_NAMES + (move_id + 1) * FULL_WIDTH]
        short = rom[SHORT_NAMES + move_id * SHORT_WIDTH:SHORT_NAMES + (move_id + 1) * SHORT_WIDTH]
        name, label = decode_text(full), decode_text(short)
        if b'\xff' not in full or b'\xff' not in short or not name or not label or '�' in name + label:
            raise ValueError(f'Invalid name at move-table index {move_id}.')
        names.append(name)
        short_names.append(label)
    if any(names[move_id] != expected for move_id, expected in CHECKS.items()):
        raise ValueError('Move-name identity checks failed.')
    if short_names[84] != 'ThunderShock' or short_names[800] != 'Chilly Rec.':
        raise ValueError('Battle-label identity checks failed.')
    return tuple(names), tuple(short_names)


def load_move_names(rom_path):
    path = Path(rom_path).resolve()
    stat = path.stat()
    return _load_names(str(path), stat.st_mtime_ns, stat.st_size)[0]


def enrich_party(party, rom_path):
    """Return copied party entries with moves=[{slot,id,name,pp,battle_label}].

    Empty slots (ID 0) are omitted; slot remains one-based for menu navigation.
    Unknown/out-of-range IDs are rejected rather than translated using Pokédex
    or unpatched FireRed numbering. PP comes from the caller's own-party snapshot.
    """
    if not isinstance(party, list) or len(party) > 6:
        raise ValueError('Expected at most six observed player party members.')
    path = Path(rom_path).resolve()
    stat = path.stat()
    names, labels = _load_names(str(path), stat.st_mtime_ns, stat.st_size)
    enriched = []
    for mon in party:
        if not isinstance(mon, dict):
            raise ValueError('Each party member must be an observed dictionary.')
        ids, pp = mon.get('move_ids'), mon.get('move_pp')
        if not isinstance(ids, list) or len(ids) != 4 or not isinstance(pp, list) or len(pp) != 4:
            raise ValueError('Player move IDs and PP must each contain four slots.')
        moves = []
        for slot, (move_id, points) in enumerate(zip(ids, pp), start=1):
            if type(move_id) is not int or not 0 <= move_id < MOVE_COUNT:
                raise ValueError('Player move ID is outside the verified ROM table.')
            if type(points) is not int or not 0 <= points <= 255:
                raise ValueError('Observed PP must be a byte value.')
            if move_id:
                moves.append({'slot': slot, 'id': move_id, 'name': names[move_id],
                              'pp': points, 'battle_label': labels[move_id]})
        enriched.append(dict(mon, moves=moves))
    return enriched
