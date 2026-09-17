"""Persistent observed experience for the local Radical Red controller.

This updates a SQLite experience store after each executed action. It does not
train model weights, label screenshot changes as game progress, infer hidden
opponent information, or attribute damage to a mistake rather than chance.

Integration:
    memory = ExperienceMemory()  # runtime/experience.sqlite3
    context['run_id'] = memory.active_run_id
    context['image_hash'] = sha256(screenshot_bytes).hexdigest()
    context['experience'] = memory.retrieve_lessons(context)
    # After executing input AND obtaining a fresh observation:
    feedback = memory.record_transition(before, action, after, hash_before, hash_after)

Call begin_run() only for a NEW playthrough/seed, not when restarting the app.
The active run ID is persisted automatically. All empirical observations are
isolated to that run. Explicitly general user corrections can span runs.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

DEFAULT_DATABASE = Path(__file__).resolve().parent / 'runtime' / 'experience.sqlite3'
BUTTONS = {'A', 'B', 'START', 'SELECT', 'UP', 'DOWN', 'LEFT', 'RIGHT', 'L', 'R'}
UNKNOWN = {'', 'unknown', 'not observed yet', 'unverified', 'none', 'not known'}
CONTEXT_KEYS = (
    'run_id', 'seed_id', 'objective', 'location', 'location_verified', 'scene',
    'screen_type', 'map_id', 'position', 'in_battle', 'party', 'party_verified',
    'badges', 'badges_verified', 'deaths', 'encounters', 'randomizer', 'version',
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _snapshot(context: dict) -> dict:
    if not isinstance(context, dict):
        raise ValueError('Experience context must be a dictionary.')
    result = {key: context[key] for key in CONTEXT_KEYS if key in context}
    serialized = _json(result)
    if len(serialized) > 24000:
        raise ValueError('Experience context is too large; pass only the current game snapshot.')
    # Make a deep copy so later changes to live server dictionaries cannot alter
    # the before/after observations in the middle of a database operation.
    return json.loads(serialized)


def _hash(value: Any) -> str | None:
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[A-Fa-f0-9]{16,128}', value):
        raise ValueError('Screenshot hashes must be hexadecimal digests, not image contents.')
    return value.lower()


def _run_id(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 160 or value == '*':
        raise ValueError('A run ID must be a nonempty string of at most 160 characters.')
    return value


def _action(value: dict) -> tuple[dict, str, str]:
    if not isinstance(value, dict):
        raise ValueError('An experience action must be a dictionary.')
    buttons, frames = value.get('buttons'), value.get('frames')
    if (not isinstance(buttons, list) or len(buttons) > 2
            or any(not isinstance(button, str) or button not in BUTTONS for button in buttons)
            or len(set(buttons)) != len(buttons)
            or type(frames) is not int or not 1 <= frames <= 120):
        raise ValueError('Only validated bounded gamepad actions can be recorded.')
    if any(first in buttons and second in buttons for first, second in
           (('UP', 'DOWN'), ('LEFT', 'RIGHT'), ('A', 'B'), ('START', 'SELECT'))):
        raise ValueError('Conflicting gamepad buttons cannot be recorded as an executed action.')
    buttons = sorted(buttons)
    result = {'buttons': buttons, 'frames': frames}
    for key in ('objective', 'note'):
        if isinstance(value.get(key), str):
            result[key] = value[key][:700]
    return result, _json({'buttons': buttons, 'frames': frames}), _json(buttons)


def _context_key(context: dict) -> str | None:
    # Objectives are prose and can change each decision. Do not treat them as a
    # map coordinate. A screenshot hash remains the strongest match when present.
    anchor = {}
    for key in ('location', 'scene', 'screen_type', 'map_id', 'position', 'in_battle'):
        value = context.get(key)
        if value is None or (isinstance(value, str) and value.lower().strip() in UNKNOWN):
            continue
        anchor[key] = value
    return hashlib.sha256(_json(anchor).encode()).hexdigest() if anchor else None


def _verified_party(context: dict) -> dict[str, dict]:
    if context.get('party_verified') is not True:
        return {}
    party = context.get('party', [])
    if not isinstance(party, list) or len(party) > 6:
        return {}
    result = {}
    for mon in party:
        if not isinstance(mon, dict):
            return {}
        identity, hp, max_hp = mon.get('identity'), mon.get('hp'), mon.get('max_hp')
        if (not isinstance(identity, str) or not identity or identity in result
                or type(hp) is not int or type(max_hp) is not int
                or not 0 <= hp <= max_hp or not 1 <= max_hp <= 999
                or mon.get('is_egg') is True):
            return {}
        result[identity] = mon
    return result


def _hp_observations(before: dict, after: dict) -> list[dict]:
    previous, current = _verified_party(before), _verified_party(after)
    changes = []
    for identity in previous.keys() & current.keys():
        old, new = previous[identity], current[identity]
        if old['hp'] == new['hp']:
            continue
        changes.append({
            'identity': identity,
            'pokemon': str(new.get('nickname') or new.get('species') or identity)[:80],
            'before_hp': old['hp'], 'after_hp': new['hp'],
            'delta': new['hp'] - old['hp'],
            'observed_faint': old['hp'] > 0 and new['hp'] == 0,
            'cause': 'unassigned',
        })
    return sorted(changes, key=lambda item: item['identity'])


class ExperienceMemory:
    """Thread-safe, restart-persistent observation memory with no model dependency."""

    def __init__(self, database: str | Path = DEFAULT_DATABASE):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL, seed_id TEXT, description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, run_id TEXT NOT NULL,
                    context_key TEXT, action_key TEXT NOT NULL, button_key TEXT NOT NULL,
                    image_before_hash TEXT, image_after_hash TEXT,
                    screen_changed INTEGER, unchanged INTEGER NOT NULL, unchanged_streak INTEGER NOT NULL,
                    hp_losses INTEGER NOT NULL, fainted INTEGER NOT NULL, hp_changes TEXT NOT NULL,
                    before_context TEXT NOT NULL, action TEXT NOT NULL, after_context TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS transitions_screen ON transitions(run_id,image_before_hash,id);
                CREATE INDEX IF NOT EXISTS transitions_context ON transitions(run_id,context_key,id);
                CREATE INDEX IF NOT EXISTS transitions_run ON transitions(run_id,id);
                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, run_id TEXT NOT NULL,
                    context_key TEXT, text TEXT NOT NULL, replaced_action TEXT
                );
                CREATE INDEX IF NOT EXISTS corrections_run ON corrections(run_id,id);
            ''')
            row = db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()
            if row is None:
                run_id = str(uuid.uuid4())
                db.execute('INSERT INTO runs VALUES(?,?,?,?)', (run_id, time.time(), None, 'Initial local run'))
                db.execute('INSERT INTO metadata VALUES(?,?)', ('active_run_id', run_id))

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.database, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA journal_mode=WAL')
                with db:
                    yield db
            finally:
                db.close()

    @property
    def active_run_id(self) -> str:
        with self._db() as db:
            return db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()['value']

    def begin_run(self, run_id: str | None = None, *, seed_id: str | None = None,
                  description: str = 'New playthrough') -> str:
        """Activate a new/existing explicitly named run. Never discard old history."""
        selected = _run_id(run_id or str(uuid.uuid4()))
        if seed_id is not None and (not isinstance(seed_id, str) or len(seed_id) > 160):
            raise ValueError('Seed ID must be a short string when known.')
        if not isinstance(description, str) or len(description) > 500:
            raise ValueError('Run description must be a short string.')
        with self._db() as db:
            old = db.execute('SELECT seed_id FROM runs WHERE id=?', (selected,)).fetchone()
            if old and old['seed_id'] is not None and seed_id is not None and old['seed_id'] != seed_id:
                raise ValueError('A different seed needs a different run ID.')
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?)', (selected, time.time(), seed_id, description))
            if seed_id is not None:
                db.execute('UPDATE runs SET seed_id=? WHERE id=?', (seed_id, selected))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('active_run_id',?)", (selected,))
        return selected

    def _resolve_run(self, context: dict, db) -> str:
        selected = context.get('run_id')
        if selected is None:
            selected = db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()['value']
        selected = _run_id(selected)
        seed_id = context.get('seed_id')
        if seed_id is not None and (not isinstance(seed_id, str) or len(seed_id) > 160):
            raise ValueError('Seed ID must be a short string when known.')
        existing = db.execute('SELECT seed_id FROM runs WHERE id=?', (selected,)).fetchone()
        if existing is None:
            db.execute('INSERT INTO runs VALUES(?,?,?,?)', (selected, time.time(), seed_id, 'Named run'))
        elif existing['seed_id'] is not None and seed_id is not None and existing['seed_id'] != seed_id:
            raise ValueError('Seed changed within a run; begin a new run before recording.')
        elif existing['seed_id'] is None and seed_id is not None:
            db.execute('UPDATE runs SET seed_id=? WHERE id=?', (seed_id, selected))
        return selected

    def record_transition(self, context_before: dict, action: dict, context_after: dict,
                          image_before_hash: str | None, image_after_hash: str | None) -> dict:
        """Record only an executed action paired with a fresh subsequent snapshot.

        The caller must discard canceled/stale actions and wait for the emulator
        to execute input before observing `after`. Hashes alone cannot establish
        whether a visually changed screen is progress, or why HP changed.
        """
        before, after = _snapshot(context_before), _snapshot(context_after)
        chosen, action_key, button_key = _action(action)
        first_hash, last_hash = _hash(image_before_hash), _hash(image_after_hash)
        screen_changed = first_hash != last_hash if first_hash and last_hash else None
        hp_changes = _hp_observations(before, after)
        losses = sum(change['delta'] < 0 for change in hp_changes)
        fainted = sum(change['observed_faint'] for change in hp_changes)
        # Only paired, validated HP changes can contradict an unchanged picture.
        unchanged = screen_changed is False and not hp_changes
        streak = 0
        with self._db() as db:
            run_id = self._resolve_run(before, db)
            if after.get('run_id') is not None and after['run_id'] != run_id:
                raise ValueError('Cannot join observations from different runs.')
            if before.get('seed_id') is not None and after.get('seed_id') is not None and before['seed_id'] != after['seed_id']:
                raise ValueError('Cannot join observations from different seeds.')
            self._resolve_run(dict(after, run_id=run_id), db)
            previous = db.execute('SELECT * FROM transitions WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            if unchanged:
                streak = 1
                if (previous and previous['unchanged'] and previous['button_key'] == button_key
                        and previous['image_after_hash'] == first_hash):
                    streak += previous['unchanged_streak']
            cursor = db.execute('''INSERT INTO transitions(
                created_at,run_id,context_key,action_key,button_key,image_before_hash,image_after_hash,
                screen_changed,unchanged,unchanged_streak,hp_losses,fainted,hp_changes,before_context,action,after_context
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (time.time(), run_id, _context_key(before), action_key, button_key, first_hash, last_hash,
                 screen_changed, int(unchanged), streak, losses, fainted, _json(hp_changes),
                 _json(before), _json(chosen), _json(after)))
            record_id = cursor.lastrowid
        stuck = streak >= 3
        return {
            'transition_id': record_id, 'run_id': run_id, 'screen_changed': screen_changed,
            'unchanged_streak': streak, 'stuck': stuck,
            'avoid_action': {'buttons': chosen['buttons'], 'reason': 'Repeated input produced no observed screen or validated HP change.'} if stuck else None,
            'hp_observations': hp_changes, 'observed_faints': fainted,
            'outcome': 'no_observed_change' if unchanged else 'observed_change' if screen_changed or hp_changes else 'insufficient_observation',
            'attribution': 'No cause or decision quality inferred from this observation.',
        }

    def record_correction(self, guidance: str, context: dict | None = None,
                          replaced_action: dict | None = None, *, scope: str = 'run') -> int:
        """Save an explicit user correction. General scope must be intentional."""
        if not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 2000:
            raise ValueError('A correction must contain 1–2,000 characters.')
        if scope not in ('run', 'general'):
            raise ValueError('Correction scope must be run or general.')
        context = _snapshot(context or {})
        action_json = _json(_action(replaced_action)[0]) if replaced_action is not None else None
        with self._db() as db:
            run_id = self._resolve_run(context, db) if scope == 'run' else '*'
            cursor = db.execute('INSERT INTO corrections(created_at,run_id,context_key,text,replaced_action) VALUES(?,?,?,?,?)',
                                (time.time(), run_id, _context_key(context), guidance.strip(), action_json))
            return cursor.lastrowid

    def retrieve_lessons(self, context: dict) -> dict:
        """Return bounded evidence and corrections relevant to the current run.

        Include context.image_hash (or screen_hash) for exact-screen matching.
        Without one, context matches are explicitly weaker and never prohibit a
        button solely because it failed on a different screen.
        """
        snapshot = _snapshot(context)
        current_hash = _hash(context.get('image_hash') or context.get('screen_hash'))
        context_key = _context_key(snapshot)
        with self._db() as db:
            run_id = self._resolve_run(snapshot, db)
            corrections = db.execute('''SELECT text,run_id FROM corrections WHERE run_id IN (?, '*')
                ORDER BY id DESC LIMIT 4''', (run_id,)).fetchall()
            actions, avoid, risks = [], [], []
            if current_hash or context_key:
                where = 'image_before_hash=?' if current_hash else 'context_key=?'
                matching = db.execute(f'''SELECT action_key,COUNT(*) AS attempts,
                    SUM(CASE WHEN screen_changed=1 THEN 1 ELSE 0 END) AS screen_changes,
                    SUM(unchanged) AS no_change,SUM(hp_losses) AS hp_losses,SUM(fainted) AS fainted
                    FROM transitions WHERE run_id=? AND {where}
                    GROUP BY action_key ORDER BY MAX(id) DESC LIMIT 6''',
                    (run_id, current_hash or context_key)).fetchall()
                for row in matching:
                    item = dict(json.loads(row['action_key']), attempts=row['attempts'],
                                screen_change_count=row['screen_changes'], no_change_count=row['no_change'],
                                observed_hp_loss_count=row['hp_losses'], observed_faint_count=row['fainted'])
                    actions.append(item)
                    if row['hp_losses'] or row['fainted']:
                        risks.append({'buttons': item['buttons'], 'observed_hp_loss_count': row['hp_losses'],
                                      'observed_faint_count': row['fainted'], 'cause': 'unassigned'})
            latest = db.execute('SELECT * FROM transitions WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            if (current_hash and latest and latest['image_after_hash'] == current_hash
                    and latest['unchanged_streak'] >= 3):
                avoid.append({'buttons': json.loads(latest['button_key']),
                              'unchanged_attempts': latest['unchanged_streak'],
                              'reason': 'Repeated on this identical screen without observable effect; inspect for a different safe action.'})
            run_count = db.execute('SELECT COUNT(*) FROM transitions WHERE run_id=?', (run_id,)).fetchone()[0]
        return {
            'kind': 'persistent_observed_experience', 'run_id': run_id, 'transitions_in_run': run_count,
            'match': 'exact_screenshot' if current_hash else 'same_context_only' if context_key else 'no_screen_match',
            'action_evidence': actions, 'avoid_repeating': avoid, 'stuck_on_repeated_input': bool(avoid),
            'observed_risks': risks,
            'user_corrections': [{'text': row['text'], 'scope': 'general' if row['run_id'] == '*' else 'this_run'} for row in corrections],
            'limits': 'Screen changes are not proof of progress. HP outcomes have no assigned cause. Model weights are unchanged.',
        }

    def summarize(self) -> dict:
        with self._db() as db:
            run_id = db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()['value']
            rows = db.execute('''SELECT run_id,COUNT(*) AS transitions,
                SUM(CASE WHEN screen_changed=1 THEN 1 ELSE 0 END) AS changed,
                SUM(unchanged) AS unchanged,SUM(hp_losses) AS hp_losses,SUM(fainted) AS fainted,
                SUM(CASE WHEN unchanged_streak=3 THEN 1 ELSE 0 END) AS repetitions_detected
                FROM transitions GROUP BY run_id''').fetchall()
            total_corrections = db.execute('SELECT COUNT(*) FROM corrections').fetchone()[0]
            run_count = db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        aggregate = {key: sum(row[key] or 0 for row in rows)
                     for key in ('transitions', 'changed', 'unchanged', 'hp_losses', 'fainted', 'repetitions_detected')}
        current = next((dict(row) for row in rows if row['run_id'] == run_id),
                       dict(run_id=run_id, **{key: 0 for key in aggregate}))
        return {
            'mechanism': 'Experience memory and empirical transition counts; no model-weight training.',
            'active_run_id': run_id, 'runs': run_count, 'recorded_corrections': total_corrections,
            'totals': aggregate, 'current_run': current,
            'data_scope': 'Persistent storage is local. Selected lessons are supplied to the chosen local model.',
        }


_default: ExperienceMemory | None = None
_default_lock = threading.Lock()


def _store() -> ExperienceMemory:
    global _default
    with _default_lock:
        if _default is None:
            _default = ExperienceMemory()
        return _default


def record_transition(context_before, action, context_after, image_before_hash, image_after_hash):
    return _store().record_transition(context_before, action, context_after, image_before_hash, image_after_hash)


def retrieve_lessons(context):
    return _store().retrieve_lessons(context)


def summarize():
    return _store().summarize()


def record_correction(guidance, context=None, replaced_action=None, *, scope='run'):
    return _store().record_correction(guidance, context, replaced_action, scope=scope)


def begin_run(run_id=None, *, seed_id=None, description='New playthrough'):
    return _store().begin_run(run_id, seed_id=seed_id, description=description)
