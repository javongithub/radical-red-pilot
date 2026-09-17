"""Isolated rule/persistence tests; no ROM, model, or emulator needed."""

import tempfile
from pathlib import Path
import unittest

from nuzlocke import NuzlockeLedger


def proof(detail='Independently observed in the test fixture.', source='operator_observation'):
    return {'source': source, 'verified': True, 'detail': detail}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'rules.sqlite3'
        self.ledger = NuzlockeLedger(self.path, run_id='seed-a')

    def tearDown(self):
        self.tmp.cleanup()

    def encounter(self, id='route-1-first', species='Pidgey', area='Route 1', **kwargs):
        return self.ledger.record_encounter(area, species, encounter_id=id, evidence=proof(), **kwargs)

    def active(self):
        self.ledger.set_capture_rules_active(True, evidence=proof('First Poké Balls received.'))

    def test_unknown_phase_pauses_and_preballs_do_not_spend_area(self):
        unknown = self.encounter('unknown-phase')
        self.assertTrue(unknown['pause'])
        self.ledger.resolve_encounter('unknown-phase', capture_rules_active=False, evidence=proof())
        self.ledger.set_capture_rules_active(False, evidence=proof())
        self.encounter('before-balls', species='Rattata')
        self.ledger.record_outcome('before-balls', 'failed', evidence=proof())
        self.active()
        current = self.encounter('after-balls', species='Sentret')
        self.assertTrue(current['allowed'])
        self.assertTrue(self.ledger.validate_catch('after-balls', area='Route 1', species='Sentret')['allowed'])
        self.assertFalse(self.ledger.set_capture_rules_active(False, evidence=proof())['allowed'])

    def test_first_encounter_can_take_multiple_balls_but_failure_spends_area(self):
        self.active()
        self.encounter()
        for _ in range(3):
            self.assertTrue(self.ledger.validate_catch('route-1-first', area='route 1', species='PIDGEY')['allowed'])
        self.ledger.record_outcome('route-1-first', 'failed', evidence=proof('Wild Pokémon fled.'))
        self.assertFalse(self.encounter('route-1-second', species='Rattata')['allowed'])
        self.assertEqual(self.ledger.summary()['spent_wild_areas'], ['Route 1'])

    def test_starter_and_explicit_gift_are_separate_from_wild_slot(self):
        self.active()
        self.encounter('starter', species='Bulbasaur', area='Pallet Town', kind='starter')
        self.ledger.record_outcome('starter', 'caught', pokemon_id='starter-id', evidence=proof())
        gift = self.encounter('gift', species='Eevee', area='Pallet Town', kind='gift')
        self.assertTrue(gift['pause'])
        self.ledger.configure_rules(gifts='one_separate_gift_per_area', evidence=proof())
        outcome = self.ledger.record_outcome('gift', 'caught', pokemon_id='gift-id', evidence=proof())
        self.assertTrue(outcome['allowed'])
        self.assertTrue(self.encounter('wild', species='Pidgey', area='Pallet Town')['allowed'])
        self.assertFalse(self.encounter('second-gift', species='Magikarp', area='Pallet Town', kind='gift')['allowed'])
        self.assertFalse(self.encounter('second-starter', species='Charmander', area='Lab', kind='starter')['allowed'])

    def test_duplicate_is_pending_until_explicit_skip_and_dead_species_stays_owned(self):
        self.active()
        self.encounter('first')
        self.ledger.record_outcome('first', 'caught', pokemon_id='bird-1', evidence=proof())
        self.ledger.record_faint('bird-1', evidence=proof())
        duplicate = self.encounter('dupe', area='Route 2')
        self.assertTrue(duplicate['pause'])
        self.assertIn('Previously owned', duplicate['reason'])
        self.ledger.resolve_encounter('dupe', duplicate='skip', evidence=proof('User chose the duplicate clause.'))
        self.assertTrue(self.encounter('nondupe', species='Rattata', area='Route 2')['allowed'])
        self.assertFalse(self.ledger.resolve_encounter('dupe', duplicate='count', evidence=proof())['allowed'])

    def test_counting_duplicate_and_failed_encounter_cannot_be_undone_by_new_rule(self):
        self.active()
        self.encounter('first')
        self.ledger.record_outcome('first', 'caught', pokemon_id='bird-1', evidence=proof())
        self.ledger.configure_rules(duplicates='count_as_encounter', evidence=proof())
        self.assertTrue(self.encounter('dupe', area='Route 2')['allowed'])
        self.ledger.record_outcome('dupe', 'failed', evidence=proof())
        self.ledger.configure_rules(duplicates='skip_owned_species', evidence=proof())
        self.assertFalse(self.encounter('third', species='Rattata', area='Route 2')['allowed'])

    def test_model_claim_is_not_verified_even_with_verified_true(self):
        self.active()
        observation = self.ledger.record_encounter('Route 1', 'Pidgey', encounter_id='ai-seen', evidence=proof(source='model'))
        self.assertFalse(observation['verified'])
        self.assertTrue(observation['pause'])
        self.assertTrue(self.encounter('later')['pause'])
        self.ledger.resolve_encounter('ai-seen', dismiss=True, evidence=proof('Observed text was not a wild encounter.'))
        self.assertTrue(self.ledger.validate_catch('later', area='Route 1', species='Pidgey')['allowed'])
        self.assertFalse(self.ledger.record_faint('not-fainted', evidence=proof(source='model'))['verified'])
        self.assertNotIn('not-fainted', self.ledger.snapshot()['retirements'])
        self.assertTrue(any(event['evidence']['source'] == 'model' and not event['verified'] for event in self.ledger.evidence_log()))

    def test_verified_encounter_cannot_be_dismissed_and_identity_is_stable(self):
        self.active()
        first = self.encounter()
        self.assertFalse(self.ledger.resolve_encounter(first['encounter_id'], dismiss=True, evidence=proof())['allowed'])
        with self.assertRaises(ValueError):
            self.encounter(species='Mewtwo')
        self.assertFalse(self.ledger.validate_catch(first['encounter_id'], area='Route 2', species='Pidgey')['allowed'])
        self.assertFalse(self.ledger.validate_catch(first['encounter_id'])['allowed'])

    def test_retirement_is_permanent_before_balls_and_survives_restart(self):
        self.ledger.set_capture_rules_active(False, evidence=proof())
        self.encounter('starter', species='Bulbasaur', area='Pallet Town', kind='starter')
        self.ledger.record_outcome('starter', 'caught', pokemon_id='starter-id', evidence=proof())
        self.assertTrue(self.ledger.validate_use('starter-id')['allowed'])
        self.ledger.record_faint('starter-id', evidence=proof())
        restored = NuzlockeLedger(self.path)
        self.assertEqual(restored.run_id, 'seed-a')
        self.assertFalse(restored.validate_use('starter-id')['allowed'])
        self.assertTrue(restored.validate_use('starter-id')['retired'])
        other = NuzlockeLedger(self.path, run_id='seed-b')
        self.assertEqual(other.summary()['retired'], 0)
        self.assertEqual(other.summary()['owned'], 0)

    def test_observed_illegal_catch_is_recorded_but_never_authorized_for_use(self):
        self.active()
        self.encounter('first')
        self.ledger.record_outcome('first', 'failed', evidence=proof())
        self.encounter('second', species='Rattata')
        result = self.ledger.record_outcome('second', 'caught', pokemon_id='illegal-rat', evidence=proof())
        self.assertFalse(result['allowed'])
        self.assertIn('illegal-rat', self.ledger.snapshot()['pokemon'])
        self.assertFalse(self.ledger.validate_use('illegal-rat')['allowed'])
        self.assertEqual(len(self.ledger.summary()['violations']), 1)

    def test_verified_faint_before_registration_still_blocks_later_use(self):
        self.ledger.record_faint('early-faint', evidence=proof())
        self.encounter('starter', kind='starter')
        self.ledger.record_outcome('starter', 'caught', pokemon_id='early-faint', evidence=proof())
        self.assertFalse(self.ledger.validate_use('early-faint')['allowed'])


if __name__ == '__main__':
    unittest.main()
