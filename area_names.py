"""Named Nuzlocke areas read from the exact Radical Red 4.1 ROM.

Evidence in the supplied ROM's GetMapName routine (0x080C4D78):
  0x0C4D9C stores 0xFFA80000: after shifting, the region ID bias is -88.
  0x0C4D8A compares that index with 108, accepting region IDs 88..196.
  0x0C4DB8 points to 0x083F1CAC: a table of 109 string pointers.

Labels come from that table, including RR changes such as Oak's Lab. Generic
FireRed source is only a structural cross-check, not the source of these labels:
https://github.com/pret/pokefirered/blob/master/src/region_map.c (GetMapName)
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/BPRE.ld

A named-area rule groups all map interiors with the same region name. Duplicate
region labels also share one key (e.g. the two Route 4 IDs). This module does not
infer encounter eligibility, gifts, catches, or Nuzlocke exceptions. An unknown
or unvalidated location has no usable ledger key. The Celadon department-store
popup override is deliberately not a separate encounter area from Celadon City.
"""
from functools import lru_cache
import hashlib
from pathlib import Path
import struct
import unicodedata

from telemetry import ROM_SHA256, decode_text

REGION_FIRST = 88
REGION_COUNT = 109
TABLE_OFFSET = 0x003F1CAC
POINTER_OFFSET = 0x000C4DB8
NAME_MAX_BYTES = 48
CHECKS = {
    88: 'Pallet Town', 89: 'Viridian City', 90: 'Pewter City',
    99: 'Route 4', 101: 'Route 1', 102: 'Route 2', 104: 'Route 4',
    126: 'Viridian Forest', 127: 'Mt. Moon',
    157: "Oak's Lab", 188: "Oak's Lab", 196: 'Celadon Dept.',
}


def _label(raw):
    # The ROM's left/right single-quote glyphs are both used as apostrophes.
    return unicodedata.normalize('NFC', raw).replace('‘', "'").replace('’', "'")


@lru_cache(maxsize=2)
def _load(path, modified_ns, size):
    rom = Path(path).read_bytes()
    if hashlib.sha256(rom).hexdigest() != ROM_SHA256:
        raise ValueError('Unsupported ROM fingerprint; area labels must be revalidated.')
    if struct.unpack_from('<I', rom, POINTER_OFFSET)[0] != TABLE_OFFSET + 0x08000000:
        raise ValueError('Region-name table pointer check failed.')
    if struct.unpack_from('<I', rom, 0x0C4D9C)[0] != 0xFFA80000:
        raise ValueError('Region-name index bias check failed.')
    if rom[0x0C4D8A:0x0C4D8E] != bytes.fromhex('6c 2d 16 d8'):
        raise ValueError('Region-name bounds check failed.')
    if rom[0x0C4DA4:0x0C4DAC] != bytes.fromhex('04 48 a9 00 09 18 09 68'):
        raise ValueError('Region-name table indexing check failed.')
    entries = []
    for index in range(REGION_COUNT):
        pointer = struct.unpack_from('<I', rom, TABLE_OFFSET + index * 4)[0]
        offset = pointer - 0x08000000
        if not 0 <= offset <= len(rom) - NAME_MAX_BYTES:
            raise ValueError(f'Region {index + REGION_FIRST} name pointer is outside ROM.')
        raw = rom[offset:offset + NAME_MAX_BYTES]
        if b'\xff' not in raw:
            raise ValueError(f'Region {index + REGION_FIRST} name has no bounded terminator.')
        raw_name = decode_text(raw)
        if not raw_name or '�' in raw_name:
            raise ValueError(f'Region {index + REGION_FIRST} name is undecodable.')
        entries.append((index + REGION_FIRST, _label(raw_name), raw_name))
    labels = {region: name for region, name, _ in entries}
    if any(labels.get(region) != name for region, name in CHECKS.items()):
        raise ValueError('Region-name identity checks failed.')
    return tuple(entries)


def _entries(rom_path):
    path = Path(rom_path).resolve()
    stat = path.stat()
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def load_area_names(rom_path):
    """Return a fresh dict mapping region IDs to names; reject other ROM builds."""
    return {region: name for region, name, _ in _entries(rom_path)}


def region_name(region_id, rom_path):
    """Return this ROM's named region, or None for unknown/invalid IDs."""
    names = load_area_names(rom_path)
    return names.get(region_id) if type(region_id) is int else None


def describe_area(world, rom_path):
    """Resolve a world snapshot for display and a named-area encounter ledger.

    Returns name even when the caller's region byte is not yet live-validated,
    but supplies area_key only when world.valid and world.validated are both
    exactly True. A key identifies a location; it does not authorize a catch.
    Map numbers/floors never contribute to the key. Same-name region aliases
    use the smallest region ID, so Route 4's IDs 99 and 104 cannot create two
    catches under the named-area rule.
    """
    result = {'name': None, 'raw_name': None, 'region_id': None,
              'area_key': None, 'same_named_region_ids': [], 'verified': False,
              'source': 'fingerprinted ROM region-name table', 'status': 'unknown'}
    if not isinstance(world, dict):
        return result
    region = world.get('region_id')
    result['region_id'] = region if type(region) is int else None
    entries = _entries(rom_path)
    match = next((entry for entry in entries if type(region) is int and entry[0] == region), None)
    if match is None:
        return result
    _, name, raw_name = match
    aliases = [other for other, other_name, _ in entries if other_name.casefold() == name.casefold()]
    verified = world.get('valid') is True and world.get('validated') is True
    result.update(name=name, raw_name=raw_name, same_named_region_ids=aliases,
                  verified=verified, status='verified' if verified else 'location_not_validated')
    if verified:
        result['area_key'] = f'rr41:area:{min(aliases)}'
    return result


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('rom', type=Path)
    parser.add_argument('--region', type=int)
    args = parser.parse_args()
    result = load_area_names(args.rom) if args.region is None else region_name(args.region, args.rom)
    print(json.dumps(result, ensure_ascii=False, indent=2))
