"""Isolated naming-screen tests; no ROM, model, or emulator needed."""

import unittest

from naming import NamingSession, is_naming_screen


def screen(text, valid=True):
    return {'valid': valid, 'text': text, 'lines': [], 'continue_indicator': {'visible': False}}


# Captured verbatim from the live nickname keyboard, OCR warts included.
LIVE = ("Tilve DOK DBnck\nTreecko's nickname?\n-=-:\nabe\nde f\nlothers\nSELECTD\n"
        "g hi\nJk]\nBACK]\nP9*8\nВІВUTTOH\ntuy\n•* У Z\nSTARTD")
OVERWORLD = screen('')
DIALOGUE = screen('Oak: This Pokemon is really quite energetic!')


class DetectionTests(unittest.TestCase):
    def test_live_naming_screen_is_detected(self):
        self.assertTrue(is_naming_screen(screen(LIVE)))

    def test_prompt_with_a_space_is_detected(self):
        self.assertTrue(is_naming_screen(screen("Treecko's nick name?")))

    def test_misread_prompt_falls_back_to_keyboard_hints(self):
        self.assertTrue(is_naming_screen(screen('Tilve DOK DBnck\nSELECTD\nBACK]\nSTARTD')))

    def test_overworld_and_dialogue_are_not_naming(self):
        self.assertFalse(is_naming_screen(OVERWORLD))
        self.assertFalse(is_naming_screen(DIALOGUE))

    def test_invalid_read_is_never_naming(self):
        self.assertFalse(is_naming_screen(screen(LIVE, valid=False)))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = NamingSession()

    def test_types_a_character_then_confirms(self):
        first = self.session.choose(screen(LIVE))
        self.assertEqual(first['buttons'], ['A'])
        second = self.session.choose(screen(LIVE))
        self.assertEqual(second['buttons'], ['START'])

    def test_retries_the_pair_while_the_screen_persists(self):
        seen = [self.session.choose(screen(LIVE))['buttons'] for _ in range(4)]
        self.assertEqual(seen, [['A'], ['START'], ['A'], ['START']])

    def test_gives_up_honestly_rather_than_looping(self):
        outcome = None
        for _ in range(20):
            outcome = self.session.choose(screen(LIVE))
            if outcome['pause']:
                break
        self.assertTrue(outcome['pause'])
        self.assertEqual(outcome['buttons'], [])
        self.assertIn('Take over', outcome['note'])

    def test_returns_nothing_once_the_screen_is_gone(self):
        self.session.choose(screen(LIVE))
        self.assertIsNone(self.session.choose(DIALOGUE))

    def test_leaving_the_screen_resets_the_attempt_count(self):
        for _ in range(3):
            self.session.choose(screen(LIVE))
        self.session.choose(DIALOGUE)
        self.assertEqual(self.session.attempts, 0)
        self.assertEqual(self.session.choose(screen(LIVE))['buttons'], ['A'])

    def test_a_zero_attempt_session_is_rejected(self):
        with self.assertRaises(ValueError):
            NamingSession(max_attempts=0)


if __name__ == '__main__':
    unittest.main()
