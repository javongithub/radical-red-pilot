"""Isolated battle-policy tests; no ROM, model, or emulator needed."""

import unittest

from battle import (choose_battle_action, effectiveness, in_danger, move_buttons,
                    rank_moves, score_move)


def move(name='Pound', type='Normal', power=40, category='physical', slot=1, pp=35, accuracy=100):
    return {'name': name, 'type': type, 'power': power, 'category': category,
            'slot': slot, 'pp': pp, 'accuracy': accuracy}


def treecko(hp=19, max_hp=19, moves=None):
    return {'side': 'player', 'species': 'Treecko', 'nickname': 'Treecko', 'types': ['Grass'],
            'level': 5, 'hp': hp, 'max_hp': max_hp,
            'stats': {'attack': 10, 'defense': 8, 'speed': 12, 'sp_attack': 11, 'sp_defense': 10},
            'moves': moves if moves is not None else [
                move('Pound', 'Normal', 40, 'physical', 1),
                move('Leer', 'Normal', 0, 'status', 2, pp=30),
                move('Absorb', 'Grass', 20, 'special', 3, pp=25)]}


def foe(species='Geodude', types=('Rock', 'Ground')):
    return {'side': 'opponent', 'species': species, 'types': list(types), 'level': 5,
            'hp_bar_fraction': 1.0}


def battle(mine=None, theirs=None, **kwargs):
    base = {'valid': True, 'validated': True, 'in_battle': True, 'trainer_battle': True,
            'combatants': [mine or treecko(), theirs or foe()]}
    base.update(kwargs)
    return base


class EffectivenessTests(unittest.TestCase):
    def test_grass_doubles_into_rock(self):
        self.assertEqual(effectiveness('Grass', ['Rock']), 2)

    def test_dual_types_multiply(self):
        # Rock/Ground is 4x weak to Grass — exactly Brock's Geodude line.
        self.assertEqual(effectiveness('Grass', ['Rock', 'Ground']), 4)

    def test_immunity_is_zero(self):
        self.assertEqual(effectiveness('Normal', ['Ghost']), 0)
        self.assertEqual(effectiveness('Electric', ['Ground']), 0)

    def test_modern_chart_is_used(self):
        self.assertEqual(effectiveness('Ghost', ['Steel']), 1)   # Steel lost this resistance
        self.assertEqual(effectiveness('Fairy', ['Dragon']), 2)

    def test_unknown_type_is_neutral(self):
        self.assertEqual(effectiveness('Unknown', ['Rock']), 1)

    def test_a_missing_move_type_is_rejected(self):
        with self.assertRaises(ValueError):
            effectiveness(None, ['Rock'])


class ScoringTests(unittest.TestCase):
    def test_super_effective_stab_beats_stronger_neutral_move(self):
        ranked = rank_moves(battle())
        self.assertEqual(ranked[0]['name'], 'Absorb')
        self.assertEqual(ranked[0]['effectiveness'], 4)

    def test_status_moves_rank_below_damage(self):
        ranked = rank_moves(battle())
        self.assertEqual(ranked[-1]['name'], 'Leer')
        self.assertEqual(ranked[-1]['score'], 0.0)

    def test_a_move_without_pp_is_ranked_last(self):
        mine = treecko(moves=[move('Absorb', 'Grass', 20, 'special', 1, pp=0), move()])
        self.assertEqual(rank_moves(battle(mine=mine))[0]['name'], 'Pound')

    def test_an_immune_target_scores_zero(self):
        result = score_move(move('Pound', 'Normal'), treecko(), foe('Gengar', ('Ghost',)))
        self.assertEqual(result['score'], 0.0)
        self.assertIn('immune', result['reason'])

    def test_accuracy_reduces_the_score(self):
        accurate = score_move(move(accuracy=100), treecko(), foe())
        shaky = score_move(move(accuracy=50), treecko(), foe())
        self.assertGreater(accurate['score'], shaky['score'])

    def test_physical_and_special_use_the_matching_stat(self):
        physical = score_move(move('X', 'Normal', 40, 'physical'), treecko(), foe())
        special = score_move(move('Y', 'Normal', 40, 'special'), treecko(), foe())
        self.assertNotEqual(physical['score'], special['score'])


class CursorTests(unittest.TestCase):
    def test_every_slot_homes_before_moving(self):
        self.assertEqual(move_buttons(1), ['LEFT', 'UP', 'A'])
        self.assertEqual(move_buttons(2), ['LEFT', 'UP', 'RIGHT', 'A'])
        self.assertEqual(move_buttons(3), ['LEFT', 'UP', 'DOWN', 'A'])
        self.assertEqual(move_buttons(4), ['LEFT', 'UP', 'RIGHT', 'DOWN', 'A'])

    def test_an_invalid_slot_is_rejected(self):
        for bad in (0, 5, None, 'one'):
            with self.assertRaises(ValueError):
                move_buttons(bad)


class ActionTests(unittest.TestCase):
    def test_picks_the_best_move_against_a_rock_type(self):
        action = choose_battle_action(battle())
        self.assertEqual(action['status'], 'move')
        self.assertEqual(action['name'], 'Absorb')
        self.assertEqual(action['buttons'], ['LEFT', 'UP', 'DOWN', 'A'])

    def test_low_hp_pauses_because_a_faint_is_permanent(self):
        action = choose_battle_action(battle(mine=treecko(hp=5)))
        self.assertEqual(action['status'], 'paused')
        self.assertIn('permanent', action['reason'])

    def test_no_useful_move_pauses(self):
        mine = treecko(moves=[move('Pound', 'Normal', 40, 'physical', 1)])
        action = choose_battle_action(battle(mine=mine, theirs=foe('Gengar', ('Ghost',))))
        self.assertEqual(action['status'], 'paused')

    def test_outside_a_battle_it_declines(self):
        self.assertEqual(choose_battle_action({'valid': True, 'in_battle': False})['status'],
                         'not_in_battle')

    def test_an_unreadable_battle_pauses(self):
        action = choose_battle_action({'valid': True, 'in_battle': True, 'combatants': []})
        self.assertEqual(action['status'], 'paused')

    def test_healthy_pokemon_is_not_in_danger(self):
        self.assertFalse(in_danger(battle()))
        self.assertTrue(in_danger(battle(mine=treecko(hp=6))))


if __name__ == '__main__':
    unittest.main()


from battle import BattleSession, is_action_menu, is_move_menu


def scr(text, valid=True):
    return {'valid': valid, 'text': text}


class MenuDetectionTests(unittest.TestCase):
    def test_action_menu_is_detected(self):
        self.assertTrue(is_action_menu(scr('FIGHT  BAG\nPOKéMON  RUN')))

    def test_partial_ocr_still_detects_the_action_menu(self):
        self.assertTrue(is_action_menu(scr('FIGHT\nPOKeMON')))

    def test_battle_dialogue_is_not_the_action_menu(self):
        self.assertFalse(is_action_menu(scr('You are challenged by Rival Aaaaaa!')))

    def test_move_menu_matches_our_own_move_names(self):
        moves = [{'name': 'Pound'}, {'name': 'Leer'}, {'name': 'Absorb'}]
        self.assertTrue(is_move_menu(scr('POUND  LEER\nABSORB'), moves))
        self.assertFalse(is_move_menu(scr('You are challenged by Rival!'), moves))

    def test_one_matching_name_is_not_enough(self):
        self.assertFalse(is_move_menu(scr('Treecko used Pound!'), [{'name': 'Pound'}, {'name': 'Leer'}]))


class SessionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.session = BattleSession()

    def test_battle_text_is_advanced_with_a(self):
        action = self.session.choose(battle(), scr('You are challenged by Rival Aaaaaa!'))
        self.assertEqual(action['buttons'], ['A'])

    def test_action_menu_selects_fight_by_homing(self):
        seen = []
        for _ in range(3):
            seen.append(self.session.choose(battle(), scr('FIGHT BAG POKeMON RUN'))['buttons'][0])
        self.assertEqual(seen, ['LEFT', 'UP', 'A'])

    def test_move_menu_walks_to_the_chosen_slot(self):
        # Absorb is slot 3 and best against Rock/Ground, so: home, DOWN, A.
        screen = scr('POUND LEER ABSORB')
        seen = [self.session.choose(battle(), screen)['buttons'][0] for _ in range(4)]
        self.assertEqual(seen, ['LEFT', 'UP', 'DOWN', 'A'])

    def test_a_confirmed_dangerous_position_pauses_instead_of_attacking(self):
        low = battle(mine=treecko(hp=4))
        self.session.choose(low, scr('FIGHT BAG POKeMON RUN'))   # first sample re-reads
        action = self.session.choose(low, scr('FIGHT BAG POKeMON RUN'))
        self.assertTrue(action['pause'])
        self.assertEqual(action['buttons'], [])

    def test_leaving_the_battle_clears_the_queue(self):
        self.session.choose(battle(), scr('FIGHT BAG POKeMON RUN'))
        self.assertIsNone(self.session.choose({'valid': True, 'in_battle': False}, scr('')))
        self.assertEqual(self.session.queue, [])


class DangerConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.session = BattleSession()

    def test_a_single_low_reading_does_not_pause(self):
        # A live read reported 2/19 mid-animation for a Pokemon actually on 12/19.
        action = self.session.choose(battle(mine=treecko(hp=2)), scr('FIGHT BAG POKeMON RUN'))
        self.assertFalse(action['pause'])

    def test_two_consecutive_low_readings_do_pause(self):
        low = battle(mine=treecko(hp=2))
        self.session.choose(low, scr('FIGHT BAG POKeMON RUN'))
        action = self.session.choose(low, scr('FIGHT BAG POKeMON RUN'))
        self.assertTrue(action['pause'])
        self.assertIn('permanent', action['note'])

    def test_a_healthy_reading_clears_the_suspicion(self):
        self.session.choose(battle(mine=treecko(hp=2)), scr('FIGHT BAG POKeMON RUN'))
        self.session.choose(battle(), scr('FIGHT BAG POKeMON RUN'))
        self.assertEqual(self.session.danger_samples, 0)


class DangerReReadTests(unittest.TestCase):
    def test_the_re_read_never_costs_a_turn(self):
        # Pressing A while re-reading confirms a move and hands the opponent a
        # turn; that is how a Pokemon was actually lost at 4 HP.
        session = BattleSession()
        action = session.choose(battle(mine=treecko(hp=2)), scr('FIGHT BAG POKeMON RUN'))
        self.assertEqual(action['buttons'], [])
        self.assertFalse(action['pause'])
