"""Local, read-only battle facts for the fingerprinted Radical Red 4.1 ROM.

Layouts: CFRU include/pokemon.h (BattleMove, BaseStats, BattlePokemon).
This ROM's header pointers 0x1BC/0x1CC reference the active expanded tables.
Enemy moves, RNG, and unrevealed trainer party members are never exposed.
"""
from functools import lru_cache
from pathlib import Path
import struct

from telemetry import load_rom_metadata, decode_text
from battle_data import load_move_names

TYPES = {0:'Normal',1:'Fighting',2:'Flying',3:'Poison',4:'Ground',5:'Rock',
         6:'Bug',7:'Ghost',8:'Steel',9:'Mystery',10:'Fire',11:'Water',12:'Grass',
         13:'Electric',14:'Psychic',15:'Ice',16:'Dragon',17:'Dark',23:'Fairy'}


@lru_cache(maxsize=2)
def tables(rom_path):
    metadata = load_rom_metadata(str(Path(rom_path).resolve()))
    rom = Path(rom_path).read_bytes()
    base = struct.unpack_from('<I',rom,0x1BC)[0] - 0x08000000
    moves = struct.unpack_from('<I',rom,0x1CC)[0] - 0x08000000
    if (base,moves) != (0x17B98EC,0x11521D0):
        raise ValueError('Battle table pointers do not match the validated ROM.')
    if (rom[base+28:base+28+8] != bytes([45,49,49,45,65,65,12,3])
            or rom[moves+13:moves+17] != bytes([40,0,100,35])
            or rom[moves+25:moves+29] != bytes([50,1,100,25])):
        raise ValueError('Battle table identity checks failed.')
    return rom, base, moves, metadata['species_names'], load_move_names(rom_path)


def species_info(species_id,rom_path):
    rom,base,_,names,_ = tables(str(rom_path))
    if type(species_id) is not int or not 0 < species_id < len(names):
        raise ValueError('Unknown species ID.')
    record = rom[base+28*species_id:base+28*(species_id+1)]
    types = list(dict.fromkeys(TYPES.get(i,'Unknown') for i in record[6:8]))
    return {'species':names[species_id], 'types':types,
            'base_stats':dict(zip(('hp','attack','defense','speed','sp_attack','sp_defense'),record[:6])),
            'catch_rate':record[8], 'source':'fingerprinted ROM species table'}


def move_info(move_id,rom_path):
    rom,_,base,_,names = tables(str(rom_path))
    if type(move_id) is not int or not 0 < move_id < len(names):
        raise ValueError('Unknown move ID.')
    b = rom[base+12*move_id:base+12*(move_id+1)]
    return {'id':move_id,'name':names[move_id], 'power':b[1], 'type':TYPES.get(b[2],'Unknown'),
            'accuracy':b[3], 'base_pp':b[4], 'priority':struct.unpack('b',b[7:8])[0],
            'category':{0:'physical',1:'special',2:'status'}.get(b[10],'unknown'),
            'effect_id':b[0], 'source':'fingerprinted ROM move table'}


def enrich_party(party,rom_path):
    result=[]
    for mon in party:
        facts=species_info(mon['species_id'],rom_path)
        moves=[dict(move_info(m['id'],rom_path),slot=m['slot'],pp=m['pp'],battle_label=m['battle_label'])
               for m in mon.get('moves',[])]
        result.append(dict(mon,types=facts['types'],moves=moves))
    return result


def _observe_battle(read_memory,rom_path,*,validated=False):
    """Candidate observed combatants; must be matched to a live battle first.

    Own stats/PP are available to a player. Opponent exposes only displayed
    species/level, approximate HP bar and static species types. No hidden moves.
    """
    if not (read_memory(0x03003529,1)[0] & 2):
        return {'valid':True,'validated':validated,'in_battle':False}
    count=read_memory(0x02023BCC,1)[0]
    if count not in (2,4):
        return {'valid':False,'validated':False,'in_battle':True,'error':'Battle transition'}
    positions=read_memory(0x02023BD6,count)
    if len(set(positions))!=count or any(p>3 for p in positions):
        raise ValueError('Invalid battler positions')
    raw=read_memory(0x02023BE4,88*count)
    mons=[]
    for i in range(count):
        b=raw[88*i:88*(i+1)]
        species=struct.unpack_from('<H',b)[0]
        hp=struct.unpack_from('<H',b,40)[0]; maximum=struct.unpack_from('<H',b,44)[0]
        level=b[42]
        if not (1<=level<=100 and 0<=hp<=maximum<=999 and maximum>0):
            return {'valid':False,'validated':False,'in_battle':True,'error':'Battle record is not stable'}
        facts=species_info(species,rom_path)
        own=positions[i]%2==0
        mon={'position':positions[i],'side':'player' if own else 'opponent',
             'species_id':species,'species':facts['species'],'types':facts['types'],'level':level}
        if own:
            mon.update(hp=hp,max_hp=maximum,nickname=decode_text(b[48:59]),
                       stats=dict(zip(('attack','defense','speed','sp_attack','sp_defense'),struct.unpack_from('<5H',b,2))),
                       stat_stages=list(b[25:32]))
            mon['moves']=[dict(move_info(m,rom_path),slot=j+1,pp=b[36+j])
                          for j,m in enumerate(struct.unpack_from('<4H',b,12)) if m]
        else:
            mon['hp_bar_fraction']=round(hp/maximum*48)/48
        mons.append(mon)
    flags=struct.unpack('<I',read_memory(0x02022B4C,4))[0]
    if (not(read_memory(0x03003529,1)[0]&2)
            or read_memory(0x02023BCC,1)[0]!=count
            or read_memory(0x02023BD6,count)!=positions):
        raise ValueError('Battle changed while observing')
    return {'valid':True,'validated':validated,'in_battle':True,'trainer_battle':bool(flags&8),
            'combatants':mons,'source':'read-only game memory; live screen comparison required'}


def observe_battle(read_memory,rom_path,*,validated=False):
    def exact_read(address,length):
        data=read_memory(address,length)
        if not isinstance(data,(bytes,bytearray)) or len(data)!=length:
            raise ValueError('Incomplete battle memory read')
        return bytes(data)
    try:
        return _observe_battle(exact_read,rom_path,validated=validated)
    except (ValueError,IndexError,struct.error,OSError,TimeoutError) as exc:
        return {'valid':False,'validated':False,'error':str(exc)}
