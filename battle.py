"""Deterministic move choice for a Nuzlocke, from read-only battle memory.

The local vision model cannot reliably read a battle screen, and a misplay in a
Nuzlocke is permanent. Everything needed to choose well is already in RAM via
`combat.observe_battle`: our moves with type, power, category, accuracy and PP,
plus the opponent's species types and level. So the move is picked by arithmetic
here, and the model is left out of it.

This scores moves; it does not claim to predict the battle. Opponent stats,
abilities, items and held moves are not visible, so the score is a comparison
between our own options, never a promise of a knockout.

Type chart follows the generation the hack is built on: Fairy exists, and Steel
no longer resists Ghost or Dark.
"""

from __future__ import annotations

import re

TYPES = ('Normal', 'Fighting', 'Flying', 'Poison', 'Ground', 'Rock', 'Bug', 'Ghost',
         'Steel', 'Fire', 'Water', 'Grass', 'Electric', 'Psychic', 'Ice', 'Dragon',
         'Dark', 'Fairy')
# attacker -> {defender: multiplier}; anything unlisted is 1.0.
_CHART = {
    'Normal': {'Rock': .5, 'Steel': .5, 'Ghost': 0},
    'Fighting': {'Normal': 2, 'Rock': 2, 'Steel': 2, 'Ice': 2, 'Dark': 2,
                 'Flying': .5, 'Poison': .5, 'Bug': .5, 'Psychic': .5, 'Fairy': .5, 'Ghost': 0},
    'Flying': {'Fighting': 2, 'Bug': 2, 'Grass': 2, 'Rock': .5, 'Steel': .5, 'Electric': .5},
    'Poison': {'Grass': 2, 'Fairy': 2, 'Poison': .5, 'Ground': .5, 'Rock': .5, 'Ghost': .5, 'Steel': 0},
    'Ground': {'Poison': 2, 'Rock': 2, 'Steel': 2, 'Fire': 2, 'Electric': 2,
               'Bug': .5, 'Grass': .5, 'Flying': 0},
    'Rock': {'Flying': 2, 'Bug': 2, 'Fire': 2, 'Ice': 2, 'Fighting': .5, 'Ground': .5, 'Steel': .5},
    'Bug': {'Grass': 2, 'Psychic': 2, 'Dark': 2, 'Fighting': .5, 'Flying': .5, 'Poison': .5,
            'Ghost': .5, 'Steel': .5, 'Fire': .5, 'Fairy': .5},
    'Ghost': {'Ghost': 2, 'Psychic': 2, 'Dark': .5, 'Normal': 0},
    'Steel': {'Rock': 2, 'Ice': 2, 'Fairy': 2, 'Steel': .5, 'Fire': .5, 'Water': .5, 'Electric': .5},
    'Fire': {'Bug': 2, 'Steel': 2, 'Grass': 2, 'Ice': 2, 'Rock': .5, 'Fire': .5, 'Water': .5, 'Dragon': .5},
    'Water': {'Ground': 2, 'Rock': 2, 'Fire': 2, 'Water': .5, 'Grass': .5, 'Dragon': .5},
    'Grass': {'Ground': 2, 'Rock': 2, 'Water': 2, 'Flying': .5, 'Poison': .5, 'Bug': .5,
              'Steel': .5, 'Fire': .5, 'Grass': .5, 'Dragon': .5},
    'Electric': {'Flying': 2, 'Water': 2, 'Grass': .5, 'Electric': .5, 'Dragon': .5, 'Ground': 0},
    'Psychic': {'Fighting': 2, 'Poison': 2, 'Steel': .5, 'Psychic': .5, 'Dark': 0},
    'Ice': {'Flying': 2, 'Ground': 2, 'Grass': 2, 'Dragon': 2, 'Steel': .5, 'Fire': .5, 'Water': .5, 'Ice': .5},
    'Dragon': {'Dragon': 2, 'Steel': .5, 'Fairy': 0},
    'Dark': {'Ghost': 2, 'Psychic': 2, 'Fighting': .5, 'Dark': .5, 'Fairy': .5},
    'Fairy': {'Fighting': 2, 'Dragon': 2, 'Dark': 2, 'Poison': .5, 'Steel': .5, 'Fire': .5},
}
# 2x2 move grid: slot 1 top-left, 2 top-right, 3 bottom-left, 4 bottom-right.
_SLOT_OFFSETS = {1: (), 2: ('RIGHT',), 3: ('DOWN',), 4: ('RIGHT', 'DOWN')}
HOME_BUTTONS = ('LEFT', 'UP')
STAB = 1.5
DANGER_FRACTION = 0.35


def effectiveness(move_type: str, defender_types) -> float:
    """Combined multiplier against one defender. Unknown types count as neutral."""
    if not isinstance(move_type, str):
        raise ValueError('A move type is required.')
    row = _CHART.get(move_type)
    if row is None:
        return 1.0
    total = 1.0
    for defending in defender_types or ():
        total *= row.get(defending, 1.0)
    return total


def score_move(move: dict, attacker: dict, defender: dict) -> dict:
    """Relative value of one move. Status moves rank below any damaging option."""
    if not isinstance(move, dict):
        raise ValueError('A move record is required.')
    power = move.get('power') or 0
    accuracy = move.get('accuracy') or 100
    category = move.get('category')
    multiplier = effectiveness(move.get('type'), defender.get('types'))
    if not move.get('pp'):
        return {'slot': move.get('slot'), 'name': move.get('name'), 'score': -1.0,
                'effectiveness': multiplier, 'reason': 'No PP left.'}
    if category == 'status' or not power:
        return {'slot': move.get('slot'), 'name': move.get('name'), 'score': 0.0,
                'effectiveness': multiplier, 'reason': 'Status move; no direct damage.'}
    if multiplier == 0:
        return {'slot': move.get('slot'), 'name': move.get('name'), 'score': 0.0,
                'effectiveness': 0.0, 'reason': 'The target is immune to this type.'}
    stats = attacker.get('stats') or {}
    offence = stats.get('attack', 1) if category == 'physical' else stats.get('sp_attack', 1)
    same_type = STAB if move.get('type') in (attacker.get('types') or ()) else 1.0
    score = power * multiplier * same_type * (min(accuracy, 100) / 100.0) * max(offence, 1)
    return {'slot': move.get('slot'), 'name': move.get('name'), 'score': round(score, 2),
            'effectiveness': multiplier,
            'reason': '%s %s at %gx vs %s' % (move.get('type'), category, multiplier,
                                              '/'.join(defender.get('types') or ['?']))}


def _sides(battle: dict):
    combatants = battle.get('combatants') or []
    mine = [m for m in combatants if m.get('side') == 'player']
    theirs = [m for m in combatants if m.get('side') == 'opponent']
    return (mine[0] if mine else None), (theirs[0] if theirs else None)


def rank_moves(battle: dict) -> list[dict]:
    """Every move scored, best first."""
    attacker, defender = _sides(battle)
    if attacker is None or defender is None:
        raise ValueError('Both an active Pokemon and an opponent are required.')
    scored = [score_move(move, attacker, defender) for move in attacker.get('moves') or []]
    return sorted(scored, key=lambda item: item['score'], reverse=True)


def move_buttons(slot: int) -> list[str]:
    """Buttons that select a move slot without knowing where the cursor is.

    LEFT then UP homes the 2x2 grid on slot 1 from any position, so the slot is
    reached by a fixed offset rather than by tracking cursor state.
    """
    if slot not in _SLOT_OFFSETS:
        raise ValueError('Move slot must be 1-4.')
    return list(HOME_BUTTONS) + list(_SLOT_OFFSETS[slot]) + ['A']


def in_danger(battle: dict, *, fraction: float = DANGER_FRACTION) -> bool:
    """True when our active Pokemon is low enough that a loss could be permanent."""
    attacker, _ = _sides(battle)
    if not attacker or not attacker.get('max_hp'):
        return False
    return attacker['hp'] / attacker['max_hp'] <= fraction


def choose_battle_action(battle: dict) -> dict:
    """Pick the next battle decision, or pause when a Nuzlocke life is at risk."""
    if not battle.get('valid') or not battle.get('in_battle'):
        return {'status': 'not_in_battle'}
    attacker, defender = _sides(battle)
    if attacker is None or defender is None:
        return {'status': 'paused', 'reason': 'Battle participants are not readable yet.'}
    if in_danger(battle):
        return {'status': 'paused',
                'reason': '%s is at %d/%d HP. A faint is permanent in this run, so I stopped for you.'
                          % (attacker.get('nickname') or attacker.get('species'),
                             attacker['hp'], attacker['max_hp'])}
    ranked = rank_moves(battle)
    if not ranked or ranked[0]['score'] <= 0:
        return {'status': 'paused',
                'reason': 'No damaging move is useful against %s; decide this one yourself.'
                          % (defender.get('species') or 'this opponent')}
    best = ranked[0]
    return {'status': 'move', 'slot': best['slot'], 'name': best['name'],
            'buttons': move_buttons(best['slot']), 'ranked': ranked,
            'reason': 'Using %s: %s.' % (best['name'], best['reason'])}


# The battle menus are the one place OCR is dependable: these labels are large,
# high contrast and fixed. Detecting which menu is up avoids tracking cursor
# state across frames, which the 2-second snapshot interval cannot do reliably.
ACTION_LABELS = (re.compile(r'FIGHT', re.I), re.compile(r'\bBAG\b', re.I),
                 re.compile(r'POK', re.I), re.compile(r'\bRUN\b', re.I))
MIN_ACTION_LABELS = 2
FIGHT_BUTTONS = list(HOME_BUTTONS) + ['A']


def is_action_menu(screen: dict) -> bool:
    """True when FIGHT / BAG / POKEMON / RUN is showing."""
    if not screen.get('valid'):
        return False
    text = str(screen.get('text') or '')
    return sum(bool(label.search(text)) for label in ACTION_LABELS) >= MIN_ACTION_LABELS


def is_move_menu(screen: dict, moves) -> bool:
    """True when the move list is showing, matched against our own move names."""
    if not screen.get('valid'):
        return False
    text = str(screen.get('text') or '').lower()
    names = [str(m.get('name') or '').lower() for m in (moves or []) if m.get('name')]
    return sum(bool(name and name in text) for name in names) >= 2


class BattleSession:
    """Executes one battle decision as single button presses.

    The pilot issues one input per decision, so a planned sequence is handed out
    one button at a time and re-derived from the screen each turn rather than
    assumed. Dialogue between turns is advanced with A.
    """

    # The HP in battle memory passes through intermediate values while the
    # damage bar animates: a live read reported 2/19 for a Pokemon that settled
    # at 12/19. One low sample therefore is not evidence of danger.
    DANGER_CONFIRMATIONS = 2

    def __init__(self):
        self.queue: list[str] = []
        self.danger_samples = 0

    def reset(self) -> None:
        self.queue = []
        self.danger_samples = 0

    def choose(self, battle: dict, screen: dict) -> dict | None:
        if not battle.get('valid') or not battle.get('in_battle'):
            self.reset()
            return None
        if self.queue:
            return self._press(self.queue.pop(0), 'Continuing the planned battle input.')
        if in_danger(battle):
            self.danger_samples += 1
            if self.danger_samples < self.DANGER_CONFIRMATIONS:
                # Must not press A here: in battle that confirms a move and gives
                # the opponent a turn. Re-reading has to cost nothing, or the
                # confirmation itself can get the Pokemon killed. It did once.
                return {'buttons': [], 'frames': 1, 'pause': False, 'events': [],
                        'objective': 'Win the battle without losing a Pokemon',
                        'note': 'Low HP seen once; re-reading before acting, without giving up a turn.'}
        else:
            self.danger_samples = 0
        plan = choose_battle_action(battle)
        if plan['status'] == 'paused':
            return {'buttons': [], 'frames': 1, 'pause': True, 'events': [],
                    'objective': 'Paused in battle', 'note': plan['reason']}
        if plan['status'] != 'move':
            return None
        attacker, _ = _sides(battle)
        moves = (attacker or {}).get('moves') or []
        if is_move_menu(screen, moves):
            self.queue = list(plan['buttons'])
            return self._press(self.queue.pop(0), plan['reason'])
        if is_action_menu(screen):
            self.queue = FIGHT_BUTTONS[1:]
            return self._press(FIGHT_BUTTONS[0], 'Selecting FIGHT to use %s.' % plan['name'])
        # Between turns the battle shows ordinary text; A moves it along.
        return self._press('A', 'Advancing battle text toward the next move choice.')

    @staticmethod
    def _press(button, note):
        return {'buttons': [button], 'frames': 4, 'pause': False, 'events': [],
                'objective': 'Win the battle without losing a Pokemon', 'note': note}
