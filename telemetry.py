"""Read-only, version-gated Radical Red 4.1 party telemetry.

This does not control the emulator, modify RAM, or infer catches/deaths. The
offsets are supported by the supplied ROM and CFRU source, but a live screenshot
comparison is required before passing ``validated=True`` to decode_party.

Bridge reads (address and length, decimal or hex as the bridge requires):
  party_count: 0x02024029, 1 byte
  party_data:  0x02024284, 600 bytes

ROM evidence: names pointer at 0x144 -> 0x094042CC, 11-byte entries,
0..1375. The next record is compressed graphics, not another species name.
The source Pokemon structure is plaintext, unlike original FireRed encryption:
https://github.com/Skeli789/Complete-Fire-Red-Upgrade/blob/master/include/pokemon.h
"""

from functools import lru_cache
from pathlib import Path
import hashlib
import struct

ROM_SHA256 = "679d112cdfe699c2793d82c7e7999ac9dfca9e222ad5a85d4f8f1e457cd0283f"
ROM_VERSION = "Radical Red 4.1"
PARTY_COUNT_ADDRESS = 0x02024029
PARTY_ADDRESS = 0x02024284
PARTY_RECORD_SIZE = 100
SPECIES_COUNT = 1376
READ_SPECS = {
    "party_count": {"address": PARTY_COUNT_ADDRESS, "length": 1},
    "party_data": {"address": PARTY_ADDRESS, "length": 600},
}

_CHARS = {
    0: " ", 0x1B: "é", 0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-",
    0xB0: "…", 0xB1: "“", 0xB2: "”", 0xB3: "‘", 0xB4: "’",
    0xB5: "♂", 0xB6: "♀", 0xB8: ",", 0xBA: "/", 0xF0: ":",
}
_CHARS.update({0xBB + i: chr(65 + i) for i in range(26)})
_CHARS.update({0xD5 + i: chr(97 + i) for i in range(26)})
_CHARS.update({0xA1 + i: str(i) for i in range(10)})


def decode_text(data):
    """Decode one fixed-width Gen 3 text field, stopping at the terminator."""
    return "".join(_CHARS.get(x, "�") for x in bytes(data).split(b"\xff", 1)[0]).strip()


@lru_cache(maxsize=2)
def load_rom_metadata(rom_path):
    """Validate this exact ROM and read species labels directly from its table."""
    rom = Path(rom_path).read_bytes()
    digest = hashlib.sha256(rom).hexdigest()
    if digest != ROM_SHA256:
        raise ValueError("Unsupported ROM fingerprint; party telemetry must be revalidated for this ROM.")
    base = struct.unpack_from("<I", rom, 0x144)[0] - 0x08000000
    if base != 0x014042CC:
        raise ValueError("Unexpected species-name table pointer.")
    names = tuple(decode_text(rom[base + i * 11:base + (i + 1) * 11]) for i in range(SPECIES_COUNT))
    if (names[1], names[25], names[277], names[1375]) != ("Bulbasaur", "Pikachu", "Treecko", "Chillet"):
        raise ValueError("Species-name table checks failed.")
    return {"version": ROM_VERSION, "sha256": digest, "species_names": names}


def _raw_bytes(data):
    return bytes.fromhex(data) if isinstance(data, str) else bytes(data)


def decode_party(party_count, party_data, rom_path, *, validated=False):
    """Decode a bridge snapshot. Hex strings or bytes are both accepted.

    ``validated`` means the caller has independently compared species, level,
    and HP of a live starter to the emulator display. Passing it does not bypass
    the snapshot sanity checks. ``eligible_for_events`` is false otherwise.
    The returned party is an observation only: a new member may be a gift or
    a PC withdrawal, and a missing member is not evidence of death.
    """
    result = {
        "version": ROM_VERSION, "validated": bool(validated), "valid": False,
        "eligible_for_events": False, "party": [], "errors": [],
        "status": "unverified", "source": "read-only emulator memory",
    }
    try:
        metadata = load_rom_metadata(str(Path(rom_path).resolve()))
        if not isinstance(party_count, int):
            count_bytes = _raw_bytes(party_count)
            if len(count_bytes) != 1:
                raise ValueError("Party count read must contain exactly one byte.")
            party_count = count_bytes[0]
        raw = _raw_bytes(party_data)
        if not 0 <= party_count <= 6:
            raise ValueError("Party count is outside 0..6.")
        if len(raw) != 600:
            raise ValueError("Party read must contain all 600 bytes.")
        names = metadata["species_names"]
        identities = set()
        for slot in range(party_count):
            record = raw[slot * 100:(slot + 1) * 100]
            personality, ot_id = struct.unpack_from("<II", record)
            species, item = struct.unpack_from("<HH", record, 32)
            level = record[84]
            hp, max_hp = struct.unpack_from("<HH", record, 86)
            identity = f"{ot_id:08x}:{personality:08x}"
            if not 0 < species < SPECIES_COUNT or names[species] in ("", "?"):
                raise ValueError(f"Slot {slot + 1}: species field is not valid for this ROM.")
            if not 1 <= level <= 100 or not 1 <= max_hp <= 999 or not 0 <= hp <= max_hp:
                raise ValueError(f"Slot {slot + 1}: level or HP fields are inconsistent.")
            if identity in identities:
                raise ValueError("Duplicate party identity; reject potentially torn/invalid snapshot.")
            identities.add(identity)
            result["party"].append({
                "slot": slot + 1, "identity": identity,
                "species_id": species, "species": names[species],
                "nickname": decode_text(record[8:18]), "level": level,
                "hp": hp, "max_hp": max_hp, "observed_zero_hp": hp == 0,
                "held_item_id": item,
                "move_ids": list(struct.unpack_from("<4H", record, 44)),
                "move_pp": list(record[52:56]),
                "status_bits": struct.unpack_from("<I", record, 80)[0],
                "is_egg": bool(struct.unpack_from("<I", record, 72)[0] & (1 << 30)),
            })
        result.update(valid=True, eligible_for_events=bool(validated), count=party_count,
                      status="awaiting_starter" if party_count == 0 else "live" if validated else "unverified")
    except (OSError, ValueError, TypeError, struct.error) as exc:
        result["party"] = []
        result["status"] = "unavailable"
        result["errors"].append(str(exc))
    return result


def matches_visible_party(snapshot, expected):
    """Check independent visual observations before enabling live telemetry.

    expected example: [{"slot": 1, "species": "Bulbasaur", "level": 5,
                        "hp": 20, "max_hp": 20}]
    Use values read from the emulator UI, not copied from this decoder.
    """
    if not snapshot.get("valid") or not expected:
        return False
    by_slot = {mon["slot"]: mon for mon in snapshot["party"]}
    required = ("species", "level", "hp", "max_hp")
    return all(obs.get("slot") in by_slot
               and all(key in obs and obs[key] == by_slot[obs["slot"]][key] for key in required)
               for obs in expected)
