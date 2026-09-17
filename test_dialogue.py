"""Isolated screen-text policy tests; no ROM, model, or emulator needed."""

import unittest

from dialogue import (choose_dialogue_action, dialogue_is_active, menu_is_open,
                      menu_lines_above_dialogue, real_text_lines)


def line(text='START', confidence=0.95, x=20.0, y=120.0, width=40.0, height=10.0):
    return {'text': text, 'confidence': confidence,
            'box': {'x': x, 'y': y, 'width': width, 'height': height}}


def screen(lines=(), arrow=False, valid=True):
    return {'valid': valid, 'lines': list(lines), 'text': '\n'.join(l.get('text', '') for l in lines if isinstance(l, dict)),
            'continue_indicator': {'visible': arrow}}


class TextFilterTests(unittest.TestCase):
    def test_plain_menu_text_is_kept(self):
        self.assertEqual(len(real_text_lines(screen([line()]))), 1)

    def test_scenery_misread_is_discarded(self):
        # The live false positive: tree tops read as "toasa", 0.30 confidence,
        # a 38px-tall box starting just above the top of a 160px screen.
        noise = line('toasa', confidence=0.30, x=113.7, y=-0.67, width=15.9, height=38.3)
        self.assertEqual(real_text_lines(screen([noise])), [])

    def test_low_confidence_alone_is_enough_to_discard(self):
        self.assertEqual(real_text_lines(screen([line(confidence=0.2)])), [])

    def test_implausibly_tall_line_is_discarded(self):
        self.assertEqual(real_text_lines(screen([line(height=40)])), [])

    def test_line_above_the_screen_is_discarded(self):
        self.assertEqual(real_text_lines(screen([line(y=-3)])), [])

    def test_line_past_the_bottom_is_discarded(self):
        self.assertEqual(real_text_lines(screen([line(y=155, height=20)])), [])

    def test_blank_and_malformed_lines_are_discarded(self):
        bad = [line(text='   '), {'text': 'x'}, {'box': {}, 'confidence': 0.9}, 'not a dict']
        self.assertEqual(real_text_lines(screen(bad)), [])


class MenuTests(unittest.TestCase):
    def test_a_menu_box_above_the_window_is_a_menu(self):
        self.assertTrue(menu_is_open(screen([line('POKEMON', y=40)])))

    def test_dialogue_text_is_never_a_menu(self):
        # Counting the dialogue window as a menu froze the navigator whenever
        # the continue arrow happened to be in its blink-off phase.
        self.assertFalse(menu_is_open(screen([line('Take care sweetie!', y=137)])))

    def test_a_dialogue_arrow_is_not_a_menu(self):
        self.assertFalse(menu_is_open(screen([line()], arrow=True)))

    def test_an_empty_overworld_is_not_a_menu(self):
        self.assertFalse(menu_is_open(screen([])))

    def test_scenery_noise_does_not_block_the_navigator(self):
        noise = line('toasa', confidence=0.30, x=113.7, y=-0.67, height=38.3)
        self.assertFalse(menu_is_open(screen([noise])))

    def test_an_invalid_read_is_never_a_menu(self):
        self.assertFalse(menu_is_open(screen([line()], valid=False)))


if __name__ == '__main__':
    unittest.main()


class MenuVetoTests(unittest.TestCase):
    def dialogue(self, extra=()):
        lines = [line("Oak: Let me think.", y=123), line("Just wait here.", y=138)]
        return screen(list(extra) + lines, arrow=True)

    def test_short_scenery_fragment_does_not_veto_dialogue(self):
        # A confident two-character misread from lab scenery must not block the
        # fast path; it forced every line through the slow vision model.
        noise = line('EE', confidence=1.0, y=19.0, height=10.0)
        self.assertEqual(menu_lines_above_dialogue(self.dialogue([noise])), [])
        action = choose_dialogue_action(self.dialogue([noise]), {})
        self.assertEqual(action['buttons'], ['A'])

    def test_a_real_menu_above_the_window_still_vetoes(self):
        menu = line('POKEMON', confidence=0.9, y=40.0)
        self.assertEqual(len(menu_lines_above_dialogue(self.dialogue([menu]))), 1)
        self.assertIsNone(choose_dialogue_action(self.dialogue([menu]), {}))

    def test_ordinary_dialogue_advances(self):
        self.assertEqual(choose_dialogue_action(self.dialogue(), {})['buttons'], ['A'])

    def test_dialogue_advances_without_waiting_for_the_blinking_arrow(self):
        action = choose_dialogue_action(screen([line('Hello there.', y=123)]), {})
        self.assertEqual(action['buttons'], ['A'])


class FastPathTests(unittest.TestCase):
    def window(self, arrow=False, extra=()):
        return screen(list(extra) + [line('Oak: Let me think.', y=123)], arrow=arrow)

    def test_dialogue_advances_even_while_the_arrow_is_blinking_off(self):
        self.assertEqual(choose_dialogue_action(self.window(arrow=False), {})['buttons'], ['A'])

    def test_dialogue_still_advances_when_the_arrow_shows(self):
        self.assertEqual(choose_dialogue_action(self.window(arrow=True), {})['buttons'], ['A'])

    def test_an_empty_overworld_is_not_advanced(self):
        self.assertIsNone(choose_dialogue_action(screen([]), {}))

    def test_text_only_above_the_window_is_not_dialogue(self):
        self.assertIsNone(choose_dialogue_action(screen([line('POKEMON', y=40)]), {}))

    def test_a_menu_above_the_window_still_vetoes(self):
        self.assertIsNone(choose_dialogue_action(self.window(extra=[line('POKEMON', y=40)]), {}))

    def test_consequential_text_defers_to_the_full_policy(self):
        for word in ('Charmander fainted!', 'You caught it!', 'the BOULDERBADGE', 'learned Tackle!'):
            self.assertIsNone(choose_dialogue_action(screen([line(word, y=123)]), {}), word)

    def test_battles_are_never_handled_here(self):
        self.assertIsNone(choose_dialogue_action(self.window(), {'world': {'in_battle': True}}))


class DialogueActivityTests(unittest.TestCase):
    def test_setup_warning_never_advances_as_ordinary_dialogue(self):
        # Actual OCR from the page immediately before the failed choice.
        for text in ('Please stop mashing дi and properly ansuer the incoming questions.',
                     'Would you like to play without setting custom options?',
                     'Radical Red will be played with no custom options!'):
            self.assertIsNone(choose_dialogue_action(screen([line(text, y=123)], arrow=True), {}))

    def test_a_dialogue_box_counts_even_with_the_arrow_off(self):
        self.assertTrue(dialogue_is_active(screen([line('Ah okay then!', y=122)], arrow=False)))

    def test_the_arrow_alone_counts(self):
        self.assertTrue(dialogue_is_active(screen([], arrow=True)))

    def test_an_empty_overworld_is_not_dialogue(self):
        self.assertFalse(dialogue_is_active(screen([])))

    def test_a_menu_above_the_window_is_not_dialogue(self):
        self.assertFalse(dialogue_is_active(screen([line('POKEMON', y=40)])))
