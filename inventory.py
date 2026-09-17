"""Read-only RR 4.1 Ball-pocket candidate observer, with a Bag UI truth gate.

``observe_balls(reader, rom_path, run_id=..., validation=...)`` uses four
bounded reads (480 bytes total). ``reader(address, length)`` returns bytes or
hex. Structural validity alone never enables event evidence. First compare a
positive item/count with the visible Bag using ``validate_against_bag_ui``;
the returned JSON-safe certificate is bound to this ROM, layout, and run.
The caller may persist that certificate and pass it to future observations.
An empty pocket cannot establish that Balls have never been obtained.

The supplied ROM, not unpatched FireRed save offsets, establishes this layout:
  0x08099E44 hooks SetMemoryForBagStorage -> 0x090A5F35; its Thumb routine
  copies 40 bytes from 0x09147E08 to gBagPockets at 0x0203988C.
  Third descriptor: pointer 0x0203C354, capacity 50, four bytes per slot.
  GetBagItemQuantity at 0x08099DA0 is ldrh r0,[r0]; bx lr: plain u16.
  ROM header 0x1C8 points to gItems at 0x093C0000, stride 44.

Primary source used to interpret these independently checked ROM structures:
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/src/item.c
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/include/item.h
The old SaveBlock1 bag fields are repurposed in CFRU and are never read here.
No writes, input, hidden opponent reads, inferred acquisition history, or
automatic acceptance of a language model's claim of visual verification.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path
import struct
import time
import unicodedata

from telemetry import ROM_SHA256, ROM_VERSION, decode_text

BAG_POCKETS = 0x0203988C
BALL_SLOTS = 0x0203C354
BALL_CAPACITY = 50
ITEM_TABLE = 0x013C0000
ITEM_COUNT = 750
EXPECTED_LAYOUT = (
    0x0203BB20, 450, 0x0203C228, 75, BALL_SLOTS, BALL_CAPACITY,
    0x0203C41C, 128, 0x0203C61C, 75,
)
LAYOUT_BYTES = struct.pack('<10I', *EXPECTED_LAYOUT)
LAYOUT_SIGNATURE = hashlib.sha256(LAYOUT_BYTES + b'RR4.1/plain-u16/slot4').hexdigest()
UI_SOURCES = frozenset({'operator_observation', 'user_report'})
BALL_NAMES = {
    1: 'Master Ball', 2: 'Ultra Ball', 3: 'Great Ball', 4: 'Poké Ball',
    5: 'Safari Ball', 6: 'Net Ball', 7: 'Dive Ball', 8: 'Nest Ball',
    9: 'Repeat Ball', 10: 'Timer Ball', 11: 'Luxury Ball', 12: 'Premier Ball',
    239: 'Cherish Ball', 240: 'Dusk Ball', 241: 'Heal Ball', 242: 'Quick Ball',
    243: 'Fast Ball', 244: 'Level Ball', 245: 'Lure Ball', 246: 'Heavy Ball',
    247: 'Love Ball', 248: 'Friend Ball', 249: 'Moon Ball', 250: 'Sport Ball',
    251: 'Beast Ball', 252: 'Dream Ball', 253: 'Park Ball',
}


@lru_cache(maxsize=2)
def _metadata(path, modified_ns, size):
    rom = Path(path).read_bytes()
    if hashlib.sha256(rom).hexdigest() != ROM_SHA256:
        raise ValueError('Unsupported ROM fingerprint; inventory must be revalidated.')
    checks = {
        0x99E44: bytes.fromhex('00480047355f0a09'),
        0x99DA0: bytes.fromhex('00887047'),
        0x10A5F34: bytes.fromhex('06490a0010b5064b083313cb13c213cb13c213cb13c21b68136010bd'),
        0x10A5F50: struct.pack('<II', BAG_POCKETS, 0x09147E00),
        0x1147E08: LAYOUT_BYTES,
        0x1C8: struct.pack('<I', ITEM_TABLE + 0x08000000),
    }
    if any(rom[offset:offset + len(expected)] != expected for offset, expected in checks.items()):
        raise ValueError('ROM inventory pointer, layout, or plain-quantity code check failed.')
    names = {}
    for item_id in range(ITEM_COUNT):
        offset = ITEM_TABLE + item_id * 44
        if rom[offset + 26] != 3:  # POCKET_POKE_BALLS, not menu page ordinal.
            continue
        field = rom[offset:offset + 14]
        if field[3] in (8, 9):
            text_offset = struct.unpack_from('<I', field)[0] - 0x08000000
            if not 0 <= text_offset <= len(rom) - 80:
                raise ValueError('Invalid expanded Ball item-name pointer.')
            field = rom[text_offset:text_offset + 80]
        name = decode_text(field)
        if (b'\xff' not in field or not name or '�' in name
                or struct.unpack_from('<H', rom, offset + 14)[0] != item_id
                or rom[offset + 32] != 2):
            raise ValueError('Ball item-table identity or battle-use check failed.')
        names[item_id] = name
    if names != BALL_NAMES:
        raise ValueError('Ball item-table labels differ from verified RR 4.1 data.')
    return names


def _read(reader, address, length):
    if (type(address) is not int or type(length) is not int or not 1 <= length <= 1024
            or address % 4 or not 0x02000000 <= address < address + length <= 0x02040000):
        raise ValueError('Invalid inventory read-only pointer/range.')
    value = reader(address, length)
    raw = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
    if len(raw) != length:
        raise ValueError('Short inventory memory read.')
    return raw


def _certificate_valid(certificate, run_id):
    if not isinstance(certificate, dict) or not isinstance(run_id, str) or not run_id:
        return False
    return (
        certificate.get('schema') == 1
        and certificate.get('verified') is True
        and certificate.get('source') == 'bag_ui_comparison'
        and certificate.get('source_type') in UI_SOURCES
        and certificate.get('run_id') == run_id
        and certificate.get('rom_sha256') == ROM_SHA256
        and certificate.get('layout_signature') == LAYOUT_SIGNATURE
        and isinstance(certificate.get('screen_ref'), str) and bool(certificate['screen_ref'].strip())
        and isinstance(certificate.get('detail'), str) and bool(certificate['detail'].strip())
        and isinstance(certificate.get('matched_items'), list) and bool(certificate['matched_items'])
        and all(isinstance(item, dict) and item.get('id') in BALL_NAMES
                and type(item.get('count')) is int and 1 <= item['count'] <= 65535
                and item.get('name') == BALL_NAMES[item['id']]
                for item in certificate['matched_items'])
    )


def observe_balls(read_memory, rom_path, *, run_id=None, validation=None):
    """Return a conservative Ball-pocket observation; all errors close the gate.

    ``items`` and ``candidate_total`` are candidates until ``validated`` is true.
    ``poke_ball_count``, ``has_capture_balls``, and ``current_total`` are None
    until validation. ``balls_obtained`` is only True or None: present validated
    Balls prove acquisition, but zero current stock does not disprove history.
    Feed only ``acquisition_evidence`` to a run ledger, never candidate values.
    Repeated reads detect changes during a snapshot; they cannot prove that an
    emulator independently restored an older save. The caller owns run identity.
    """
    result = {
        'version': ROM_VERSION, 'rom_sha256': ROM_SHA256, 'run_id': run_id,
        'layout_signature': LAYOUT_SIGNATURE, 'valid': False, 'validated': False,
        'status': 'unverified', 'source': 'read-only emulator memory',
        'items': [], 'candidate_total': None, 'current_total': None,
        'poke_ball_count': None, 'has_capture_balls': None, 'balls_obtained': None,
        'eligible_for_events': False, 'acquisition_evidence': None, 'errors': [],
    }
    try:
        path = Path(rom_path).resolve()
        stat = path.stat()
        names = _metadata(str(path), stat.st_mtime_ns, stat.st_size)
        before = _read(read_memory, BAG_POCKETS, 40)
        if before != LAYOUT_BYTES:
            raise ValueError('Bag descriptor is uninitialized or differs from the verified ROM layout.')
        slots = _read(read_memory, BALL_SLOTS, BALL_CAPACITY * 4)
        slots_again = _read(read_memory, BALL_SLOTS, BALL_CAPACITY * 4)
        after = _read(read_memory, BAG_POCKETS, 40)
        if slots != slots_again or before != after:
            raise ValueError('Inventory changed during snapshot; discard this torn read.')
        items, seen = [], set()
        for index, (item_id, count) in enumerate(struct.iter_unpack('<HH', slots)):
            if item_id == 0 and count == 0:
                continue
            if item_id not in names or count == 0:
                raise ValueError('Invalid item ID or empty quantity in Ball pocket.')
            if item_id in seen:
                raise ValueError('Duplicate Ball stacks need independent layout validation.')
            seen.add(item_id)
            items.append({'slot': index + 1, 'id': item_id, 'name': names[item_id], 'count': count})
        total = sum(item['count'] for item in items)
        result.update(valid=True, items=items, candidate_total=total,
                      snapshot_sha256=hashlib.sha256(before + slots).hexdigest(),
                      observed_at=time.time(), status='candidate_needs_bag_ui_validation')
        if _certificate_valid(validation, run_id):
            result.update(validated=True, current_total=total, has_capture_balls=bool(total),
                          poke_ball_count=next((x['count'] for x in items if x['id'] == 4), 0),
                          balls_obtained=True if total else None,
                          eligible_for_events=True, status='validated')
            if total:
                result['acquisition_evidence'] = {
                    'verified': True, 'source': 'validated_game_memory',
                    'detail': f'Ball pocket contains {total} Balls; layout and counts were compared with visible Bag UI.',
                    'rom_sha256': ROM_SHA256, 'run_id': run_id,
                    'snapshot_sha256': result['snapshot_sha256'],
                    'validation_screen_ref': validation['screen_ref'],
                }
        elif validation is not None:
            result['errors'].append('Bag UI validation certificate is missing, invalid, or belongs to another run.')
    except (ValueError, TypeError, OSError, struct.error) as exc:
        result.update(valid=False, validated=False, eligible_for_events=False, status='unavailable')
        result['errors'].append(str(exc))
    return result


def _label(value):
    return unicodedata.normalize('NFC', value).casefold().strip()


def validate_against_bag_ui(snapshot, visible_items, *, evidence, complete_pocket=False):
    """Create a JSON-safe certificate from a fresh independent Bag comparison.

    Example visible_items: [{"name": "Poké Ball", "count": 5}]. An optional
    ``id`` must also match. At least one positive displayed count is required;
    an empty Bag alone cannot validate the quantity representation. With
    complete_pocket=True, every nonempty Ball stack must be visible and match.

    Evidence requires verified=True, source='operator_observation' or
    'user_report', detail, and screen_ref (e.g. path to the comparison capture).
    A model's inferred OCR/description is never sufficient for this certificate.
    The caller must compare the UI against this snapshot at the same paused
    game state; 120 seconds limits accidental use of an old candidate.
    """
    if (not isinstance(snapshot, dict) or snapshot.get('valid') is not True
            or snapshot.get('rom_sha256') != ROM_SHA256
            or snapshot.get('layout_signature') != LAYOUT_SIGNATURE
            or not isinstance(snapshot.get('run_id'), str) or not snapshot['run_id']):
        raise ValueError('A valid candidate snapshot and explicit run ID are required.')
    observed_at = snapshot.get('observed_at')
    if not isinstance(observed_at, (int, float)) or not 0 <= time.time() - observed_at <= 120:
        raise ValueError('Bag comparison needs a fresh candidate snapshot (within 120 seconds).')
    if (not isinstance(evidence, dict) or evidence.get('verified') is not True
            or evidence.get('source') not in UI_SOURCES
            or not isinstance(evidence.get('detail'), str) or not evidence['detail'].strip()
            or not isinstance(evidence.get('screen_ref'), str) or not evidence['screen_ref'].strip()):
        raise ValueError('Explicit independent Bag UI evidence, detail, and screen reference are required.')
    if not isinstance(visible_items, list) or not visible_items or len(visible_items) > BALL_CAPACITY:
        raise ValueError('At least one positive Ball count must be read from the visible Bag.')
    candidates = {_label(item['name']): item for item in snapshot['items']}
    matched = []
    seen = set()
    for visible in visible_items:
        if (not isinstance(visible, dict) or not isinstance(visible.get('name'), str)
                or type(visible.get('count')) is not int or not 1 <= visible['count'] <= 65535):
            raise ValueError('Each visible Ball needs an exact name and positive integer count.')
        item = candidates.get(_label(visible['name']))
        if (item is None or visible['count'] != item['count']
                or ('id' in visible and visible['id'] != item['id']) or item['id'] in seen):
            raise ValueError('Visible Ball name/count does not match the candidate pocket.')
        seen.add(item['id'])
        matched.append({'id': item['id'], 'name': item['name'], 'count': item['count']})
    if complete_pocket and len(matched) != len(snapshot['items']):
        raise ValueError('The visible complete pocket does not match all candidate stacks.')
    return {
        'schema': 1, 'verified': True, 'source': 'bag_ui_comparison',
        'source_type': evidence['source'], 'detail': evidence['detail'].strip(),
        'screen_ref': evidence['screen_ref'].strip(), 'rom_sha256': ROM_SHA256,
        'layout_signature': LAYOUT_SIGNATURE, 'run_id': snapshot['run_id'],
        'snapshot_sha256': snapshot['snapshot_sha256'],
        'matched_items': matched, 'complete_pocket': bool(complete_pocket),
        'validated_at': time.time(),
    }
