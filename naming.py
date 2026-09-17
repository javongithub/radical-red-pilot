"""Getting off the Pokemon naming keyboard without a human.

A Nuzlocke nicknames every catch, so this screen appears constantly, and the
pilot could not clear it: the OK button is not reachable with the D-pad, only
with START, and the game ignores START while the name field is empty. The key
mask was verified to reach the emulator (mask 8 held, same as A and SELECT), so
this is game behaviour, not dropped input.

The sequence is therefore: press A to accept the character under the cursor,
then press START to confirm. The cursor starts on the first letter, so the
resulting nickname is short rather than chosen; naming a catch deliberately
would mean walking the keyboard grid, which is not needed to finish a run.
"""

from __future__ import annotations

import re

# OCR of this screen is poor ("Tilve DOK DBnck"), but the prompt line survives
# intact in every observed read, so it is the detection anchor.
PROMPT = re.compile(r'nick\s*name|\b(?:your|rival\W*s)\s+name\s*\?', re.I | re.M)
# OCR runs the button glyph into the word ("SELECTD", "STARTD"), so these are
# substring matches rather than whole words.
KEYBOARD_HINTS = (re.compile(r'SELECT', re.I), re.compile(r'BACK', re.I), re.compile(r'START', re.I))
MIN_HINTS = 2
MAX_ATTEMPTS = 4
TYPE_FRAMES = 5
CONFIRM_FRAMES = 8


def is_naming_screen(screen: dict) -> bool:
    """True when the nickname keyboard is up."""
    if not screen.get('valid'):
        return False
    text = str(screen.get('text') or '')
    if PROMPT.search(text):
        return True
    # A misread prompt still leaves the keyboard side-panel hints on screen.
    return sum(bool(hint.search(text)) for hint in KEYBOARD_HINTS) >= MIN_HINTS


class NamingSession:
    """Clears one naming screen. Construct a new session per screen."""

    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS):
        if max_attempts < 1:
            raise ValueError('At least one naming attempt is required.')
        self.max_attempts = max_attempts
        self.attempts = 0
        self.typed = False

    def reset(self) -> None:
        self.attempts = 0
        self.typed = False

    def choose(self, screen: dict) -> dict | None:
        """Next input for the naming screen, or None when it is gone."""
        if not is_naming_screen(screen):
            self.reset()
            return None
        if self.attempts >= self.max_attempts:
            return {'buttons': [], 'frames': 1, 'pause': True, 'events': [],
                    'objective': 'Paused: stuck on the naming screen',
                    'note': 'I typed a name and pressed START %d times and the naming screen did not '
                            'close. Take over so the run is not stuck here.' % self.attempts}
        if not self.typed:
            self.typed = True
            return {'buttons': ['A'], 'frames': TYPE_FRAMES, 'pause': False, 'events': [],
                    'objective': 'Name the new Pokemon',
                    'note': 'Accepting a character so the name is not empty; START is ignored on an '
                            'empty field.'}
        self.typed = False
        self.attempts += 1
        return {'buttons': ['START'], 'frames': CONFIRM_FRAMES, 'pause': False, 'events': [],
                'objective': 'Name the new Pokemon',
                'note': 'Confirming the name with START (attempt %d).' % self.attempts}
