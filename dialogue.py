"""Fast local handling of visually confirmed ordinary dialogue pages."""
import re

# Apple Vision will read shapes in the scenery as text: tree tops on a dark map
# produced "toasa" at confidence 0.30 in a box 38px tall, which is impossible for
# GBA glyphs on a 160px screen. Treating that as an open menu froze the navigator,
# so a line only counts as on-screen text when it could actually be text.
MIN_CONFIDENCE = 0.45
MAX_LINE_HEIGHT = 24
SCREEN_HEIGHT = 160
# The bottom dialogue window starts around here; anything above it is a menu
# candidate. Vision also reports confident two-character fragments from lab
# scenery ("EE"), so a menu must show at least one real word above the window.
DIALOGUE_TOP = 112
MIN_MENU_CHARS = 3


def real_text_lines(screen):
    """Lines plausible as rendered game text, discarding scenery misreads."""
    lines = screen.get('lines') or []
    kept = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        box = line.get('box') or {}
        height, top = box.get('height'), box.get('y')
        if not isinstance(height, (int, float)) or not isinstance(top, (int, float)):
            continue
        confidence = line.get('confidence')
        if not isinstance(confidence, (int, float)) or confidence < MIN_CONFIDENCE:
            continue
        if top < 0 or height > MAX_LINE_HEIGHT or top + height > SCREEN_HEIGHT:
            continue
        if not str(line.get('text', '')).strip():
            continue
        kept.append(line)
    return kept


def menu_lines_above_dialogue(screen):
    """Plausible menu text sitting above the dialogue window."""
    return [line for line in real_text_lines(screen)
            if line['box']['y'] < DIALOGUE_TOP
            and len(str(line.get('text', '')).strip()) >= MIN_MENU_CHARS]


def dialogue_window_lines(screen):
    """Text sitting inside the bottom dialogue window."""
    return [line for line in real_text_lines(screen) if line['box']['y'] >= DIALOGUE_TOP]


def menu_is_open(screen):
    """True when a menu box is up, as opposed to ordinary dialogue.

    Text inside the bottom dialogue window is dialogue, never a menu. Counting it
    as one froze the navigator every time the continue arrow blinked off.
    """
    if not screen.get('valid') or screen.get('continue_indicator', {}).get('visible'):
        return False
    return bool(menu_lines_above_dialogue(screen))


def dialogue_is_active(screen):
    """True while a dialogue box is on screen, regardless of the arrow's blink."""
    if not screen.get('valid'):
        return False
    return bool(screen.get('continue_indicator', {}).get('visible')) or bool(dialogue_window_lines(screen))

IMPORTANT = re.compile(r'faint|caught|badge|evolv|nickname|black.?out|white.?out|defeat|received|obtained|randomiz|learn|custom\s+option|without\s+setting|mashing|incoming\s+questions', re.I)


def choose_dialogue_action(screen,context):
    """Advance ordinary dialogue without spending a vision-model call.

    The continue arrow blinks and the reader states its absence is not
    conclusive, so waiting for it sent most lines to the slow path. Text inside
    the dialogue window with no menu above it is enough: A either advances the
    page or finishes printing it, and a real choice renders its own menu box,
    which the veto below still catches.
    """
    if not screen.get('valid'):
        return None
    if context.get('world',{}).get('in_battle'):
        return None
    if not dialogue_window_lines(screen):
        return None
    text=screen.get('text','').strip()
    lines=screen.get('lines',[])
    if not text or IMPORTANT.search(text) or not lines:
        return None
    # A menu or other visible text above the bottom dialogue region needs the
    # general policy. Never select an option using the dialogue shortcut.
    if menu_lines_above_dialogue(screen):
        return None
    if any(item.get('buttons')==['A'] for item in context.get('experience',{}).get('avoid_repeating',[])):
        return None
    return {'buttons':['A'],'frames':3,'pause':False,'events':[],
            'objective':'Continue the introduction' if not context.get('world') else 'Continue the current conversation',
            'note':'Continue dialogue: '+text.replace('\n',' ')[:350]}
