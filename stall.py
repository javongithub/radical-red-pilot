"""Code-enforced escape from a local model that stops making progress.

The vision model is told to keep making observable progress, but a small local
model can settle into deciding "wait for the screen to advance" forever. Prompt
text cannot be relied on to break that; this module does it in code.

Two stalls are recognised, both from decisions the controller already has:

    idle    consecutive decisions that press nothing at all
    repeat  the same button pressed on a screenshot that never changes

A stall first produces a bounded nudge (B, which backs out of a menu and never
confirms a selection), then a pause. Pausing is the honest outcome: the pilot
says it is stuck and hands control back rather than flailing at the save.
"""

from __future__ import annotations

IDLE_NUDGE = 4
IDLE_LIMIT = 8
REPEAT_NUDGE = 5
REPEAT_LIMIT = 9
NUDGE_BUTTON = 'B'
NUDGE_FRAMES = 3


def _button_key(action: dict) -> tuple[str, ...]:
    buttons = action.get('buttons')
    if not isinstance(buttons, list) or any(not isinstance(item, str) for item in buttons):
        raise ValueError('A reviewed decision needs a list of button names.')
    return tuple(sorted(buttons))


class StallMonitor:
    """Per-run progress watchdog. One instance per autopilot session."""

    def __init__(self, *, idle_nudge=IDLE_NUDGE, idle_limit=IDLE_LIMIT,
                 repeat_nudge=REPEAT_NUDGE, repeat_limit=REPEAT_LIMIT):
        if not 1 <= idle_nudge < idle_limit or not 1 <= repeat_nudge < repeat_limit:
            raise ValueError('Each nudge threshold must precede its pause limit.')
        self.idle_nudge, self.idle_limit = idle_nudge, idle_limit
        self.repeat_nudge, self.repeat_limit = repeat_nudge, repeat_limit
        self.reset()

    def reset(self) -> None:
        self.idle = 0
        self.repeat = 0
        self.nudges = 0
        self._last_key: tuple[str, ...] | None = None
        self._last_hash: str | None = None

    def status(self) -> dict:
        """Bounded facts for the model prompt. Counts observed, no cause assigned."""
        state = {'consecutive_waits': self.idle, 'repeated_identical_inputs': self.repeat,
                 'recovery_nudges': self.nudges}
        if self.idle >= self.idle_nudge - 1:
            state['directive'] = ('You have waited %d times in a row without advancing. '
                                  'Choose a real button now; waiting again is not accepted.' % self.idle)
        elif self.repeat >= self.repeat_nudge - 1:
            state['directive'] = ('The last %d identical inputs left this screenshot unchanged. '
                                  'Choose a different action or pause with a reason.' % self.repeat)
        return state

    def review(self, action: dict, *, image_hash: str | None = None) -> dict:
        """Inspect one decision before it is executed.

        Returns the action to run plus an `intervention` of None, 'nudge' or
        'pause'. The caller must journal a nudge or pause under its own source
        so the run history never credits the model with an input it did not pick.
        """
        if not isinstance(action, dict):
            raise ValueError('A decision object is required.')
        key = _button_key(action)
        if action.get('pause'):
            self.reset()
            return {'action': action, 'intervention': None, 'reason': None}
        if key:
            self.idle = 0
            same_screen = image_hash is not None and image_hash == self._last_hash
            self.repeat = self.repeat + 1 if key == self._last_key and same_screen else 1
            self._last_key, self._last_hash = key, image_hash
            if self.repeat >= self.repeat_limit:
                return self._pause('%s produced no screen change over %d attempts. I am stuck here, so '
                                   'I stopped rather than keep pressing it.' % (' + '.join(key), self.repeat))
            if self.repeat == self.repeat_nudge:
                return self._nudge('%s has not changed this screen in %d attempts; backing out with B once.'
                                   % (' + '.join(key), self.repeat))
            return {'action': action, 'intervention': None, 'reason': None}
        self.idle += 1
        self.repeat = 0
        self._last_key, self._last_hash = key, image_hash
        if self.idle >= self.idle_limit:
            return self._pause('I chose to wait %d times in a row without the run advancing. Something on '
                               'this screen is beyond what I can read, so I stopped for you.' % self.idle)
        if self.idle == self.idle_nudge:
            return self._nudge('Waited %d times with no progress; backing out with B once to unstick the screen.'
                               % self.idle)
        return {'action': action, 'intervention': None, 'reason': None}

    def _nudge(self, reason: str) -> dict:
        # An injected recovery press is not a model decision, so it must not
        # overwrite the tracked decision and restart the escalation to a pause.
        self.nudges += 1
        return {'intervention': 'nudge', 'reason': reason,
                'action': {'buttons': [NUDGE_BUTTON], 'frames': NUDGE_FRAMES, 'pause': False,
                           'events': [], 'objective': 'Recover from a stalled screen', 'note': reason}}

    def _pause(self, reason: str) -> dict:
        self.reset()
        return {'intervention': 'pause', 'reason': reason,
                'action': {'buttons': [], 'frames': 1, 'pause': True, 'events': [],
                           'objective': 'Paused: the pilot stopped making progress', 'note': reason}}
