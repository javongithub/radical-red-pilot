"""Isolated watchdog tests; no ROM, model, or emulator needed."""

import unittest

from stall import StallMonitor


def wait(frames=30):
    return {'buttons': [], 'frames': frames, 'pause': False, 'events': [],
            'objective': 'Wait', 'note': 'Waiting for the screen to advance.'}


def press(button='A', frames=3):
    return {'buttons': [button], 'frames': frames, 'pause': False, 'events': [],
            'objective': 'Advance', 'note': 'Pressing ' + button + '.'}


class IdleTests(unittest.TestCase):
    def setUp(self):
        self.monitor = StallMonitor()

    def test_occasional_wait_passes_through(self):
        for _ in range(3):
            result = self.monitor.review(wait())
            self.assertIsNone(result['intervention'])
            self.assertEqual(result['action']['buttons'], [])

    def test_fourth_consecutive_wait_is_nudged(self):
        for _ in range(3):
            self.monitor.review(wait())
        result = self.monitor.review(wait())
        self.assertEqual(result['intervention'], 'nudge')
        self.assertEqual(result['action']['buttons'], ['B'])
        self.assertFalse(result['action']['pause'])

    def test_persistent_waiting_pauses(self):
        results = [self.monitor.review(wait()) for _ in range(8)]
        self.assertEqual(results[-1]['intervention'], 'pause')
        self.assertTrue(results[-1]['action']['pause'])
        self.assertEqual(results[-1]['action']['buttons'], [])
        self.assertIn('8 times in a row', results[-1]['reason'])

    def test_real_input_clears_the_idle_streak(self):
        for _ in range(3):
            self.monitor.review(wait())
        self.monitor.review(press())
        self.assertEqual(self.monitor.idle, 0)
        self.assertIsNone(self.monitor.review(wait())['intervention'])

    def test_counters_restart_after_a_pause(self):
        for _ in range(8):
            self.monitor.review(wait())
        self.assertEqual(self.monitor.idle, 0)
        self.assertIsNone(self.monitor.review(wait())['intervention'])


class RepeatTests(unittest.TestCase):
    def setUp(self):
        self.monitor = StallMonitor()

    def test_same_button_on_a_changing_screen_is_allowed(self):
        for index in range(12):
            result = self.monitor.review(press(), image_hash='screen-%d' % index)
            self.assertIsNone(result['intervention'])

    def test_same_button_on_one_frozen_screen_is_nudged_then_paused(self):
        seen = [self.monitor.review(press(), image_hash='frozen')['intervention'] for _ in range(5)]
        self.assertEqual(seen, [None, None, None, None, 'nudge'])
        outcome = [self.monitor.review(press(), image_hash='frozen')['intervention'] for _ in range(4)]
        self.assertEqual(outcome[-1], 'pause')

    def test_unknown_hash_never_counts_as_a_repeat(self):
        for _ in range(12):
            self.assertIsNone(self.monitor.review(press())['intervention'])

    def test_a_different_button_restarts_the_repeat_count(self):
        for _ in range(4):
            self.monitor.review(press('A'), image_hash='frozen')
        self.monitor.review(press('B'), image_hash='frozen')
        self.assertEqual(self.monitor.repeat, 1)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.monitor = StallMonitor()

    def test_a_model_pause_is_never_overridden(self):
        for _ in range(3):
            self.monitor.review(wait())
        stop = dict(wait(1), pause=True, note='Uncertain encounter legality.')
        result = self.monitor.review(stop)
        self.assertIsNone(result['intervention'])
        self.assertIs(result['action'], stop)
        self.assertEqual(self.monitor.idle, 0)

    def test_status_warns_before_it_intervenes(self):
        self.assertNotIn('directive', self.monitor.status())
        for _ in range(3):
            self.monitor.review(wait())
        self.assertIn('directive', self.monitor.status())
        self.assertEqual(self.monitor.status()['consecutive_waits'], 3)

    def test_nudges_are_counted_for_the_journal(self):
        for _ in range(4):
            self.monitor.review(wait())
        self.assertEqual(self.monitor.status()['recovery_nudges'], 1)

    def test_malformed_decisions_are_rejected(self):
        for bad in (None, [], 'A', {'buttons': 'A'}, {'buttons': [1]}, {}):
            with self.assertRaises(ValueError):
                self.monitor.review(bad)

    def test_thresholds_must_escalate(self):
        with self.assertRaises(ValueError):
            StallMonitor(idle_nudge=8, idle_limit=4)
        with self.assertRaises(ValueError):
            StallMonitor(repeat_nudge=0, repeat_limit=5)


if __name__ == '__main__':
    unittest.main()
