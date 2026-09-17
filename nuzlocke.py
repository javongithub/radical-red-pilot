"""Evidence-based, persistent Nuzlocke rules for the local pilot.

This module does not inspect the emulator or turn model narration into facts.
Every write requires evidence; model/inference sources remain unverified even
when the caller sets verified=True. Use an independently verified observation
source only after actually checking the game.

Typical integration:
    ledger = NuzlockeLedger(run_id=the_existing_run_id)
    ledger.set_capture_rules_active(False, evidence=verified_new_game_observation)
    # On independently confirmed receipt of the first Poke Balls:
    ledger.set_capture_rules_active(True, evidence=verified_balls_observation)
    encounter = ledger.record_encounter(area, species, encounter_id=stable_battle_id,
                                         evidence=verified_encounter_observation)
    decision = ledger.validate_catch(encounter['encounter_id'], area=area, species=species)

Do not throw a ball if decision['allowed'] is False. Unknown legality has
pause=True. Record a failed encounter as well as a catch: its area is spent.
Starter is separate; other gifts need an explicit gift rule. Duplicate checking
is exact species only, with a default pause on known duplicates. Evolution-family
rules require verified taxonomy and are not silently inferred here.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
import unicodedata
import uuid

DEFAULT_DATABASE = Path(__file__).resolve().parent / 'runtime' / 'nuzlocke.sqlite3'
VERIFIED_SOURCES = {'user_report', 'operator_observation', 'validated_game_memory', 'verified_game_text'}
DEFAULT_RULES = {'duplicates': 'ask', 'duplicate_scope': 'exact_species', 'gifts': 'ask'}
DUPLICATE_RULES = {'ask', 'count_as_encounter', 'skip_owned_species'}
GIFT_RULES = {'ask', 'one_separate_gift_per_area'}


def _text(value, label, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label} must be a nonempty string of at most {limit} characters.')
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def _key(value, label):
    return _text(value, label).casefold()


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _evidence(value):
    if not isinstance(value, dict):
        raise ValueError('Evidence is required; model narration is not verification.')
    source = _text(value.get('source'), 'Evidence source', 80)
    detail = _text(value.get('detail'), 'Evidence detail', 1600)
    verified = value.get('verified') is True and source in VERIFIED_SOURCES
    result = {'source': source, 'detail': detail, 'verified': verified}
    for key in ('observation_id', 'screenshot_hash'):
        if value.get(key) is not None:
            result[key] = _text(value[key], key, 200)
    return result


def _answer(allowed, reason, **extra):
    return dict(allowed=bool(allowed), pause=not bool(allowed), reason=reason, **extra)


def _new_state(run_id):
    return {'run_id': run_id, 'rules': dict(DEFAULT_RULES), 'capture_rules_active': None,
            'capture_phase_evidence': None, 'encounters': {}, 'pokemon': {},
            'retirements': {}, 'violations': []}


class NuzlockeLedger:
    def __init__(self, database=DEFAULT_DATABASE, *, run_id=None):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,state TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,created_at REAL NOT NULL,
                kind TEXT NOT NULL,payload TEXT NOT NULL,evidence TEXT NOT NULL,verified INTEGER NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS ledger_events_run ON events(run_id,id)')
            row = db.execute("SELECT value FROM metadata WHERE key='active_run_id'").fetchone()
            self.run_id = _text(run_id or (row['value'] if row else str(uuid.uuid4())), 'Run ID')
            db.execute('INSERT OR IGNORE INTO runs VALUES(?,?)', (self.run_id, _json(_new_state(self.run_id))))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('active_run_id',?)", (self.run_id,))

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.database, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def _change(self, kind, payload, evidence, apply):
        proof = _evidence(evidence)
        with self._db() as db:
            state = json.loads(db.execute('SELECT state FROM runs WHERE id=?', (self.run_id,)).fetchone()['state'])
            event_id = db.execute('INSERT INTO events(run_id,created_at,kind,payload,evidence,verified) VALUES(?,?,?,?,?,?)',
                                  (self.run_id, time.time(), kind, _json(payload), _json(proof), proof['verified'])).lastrowid
            result = apply(state, proof, event_id)
            db.execute('UPDATE runs SET state=? WHERE id=?', (_json(state), self.run_id))
        return dict(result, run_id=self.run_id, event_id=event_id, verified=proof['verified'])

    def snapshot(self):
        with self._db() as db:
            return json.loads(db.execute('SELECT state FROM runs WHERE id=?', (self.run_id,)).fetchone()['state'])

    def evidence_log(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Evidence log limit must be 1–1000.')
        with self._db() as db:
            rows = db.execute('SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?', (self.run_id, limit)).fetchall()
        return [dict(row, payload=json.loads(row['payload']), evidence=json.loads(row['evidence']), verified=bool(row['verified'])) for row in rows]

    def set_capture_rules_active(self, active, *, evidence):
        if type(active) is not bool:
            raise ValueError('Capture-rule phase must be explicitly True or False.')

        def apply(state, proof, event_id):
            if not proof['verified']:
                return _answer(False, 'Poké Ball availability has not been independently confirmed.')
            if state['capture_rules_active'] is True and active is False:
                return _answer(False, 'Capture restrictions cannot be turned off after obtaining Poké Balls in this run.')
            state['capture_rules_active'] = active
            state['capture_phase_evidence'] = event_id
            return _answer(True, 'Capture restrictions are active.' if active else 'Pre-Poké Ball encounters will not spend areas.')
        return self._change('capture_phase', {'active': active}, evidence, apply)

    def configure_rules(self, *, duplicates=None, gifts=None, evidence):
        if duplicates is not None and duplicates not in DUPLICATE_RULES:
            raise ValueError('Unsupported duplicate rule.')
        if gifts is not None and gifts not in GIFT_RULES:
            raise ValueError('Unsupported gift rule.')

        def apply(state, proof, event_id):
            if not proof['verified'] or proof['source'] not in {'user_report', 'operator_observation'}:
                return _answer(False, 'A rule choice requires explicit verified user/operator evidence.')
            if duplicates is not None:
                state['rules']['duplicates'] = duplicates
                # Resolve pending duplicates only; completed prior decisions retain
                # their original resolution even if the future convention changes.
                for encounter in state['encounters'].values():
                    if encounter.get('duplicate') and encounter.get('duplicate_resolution') is None:
                        encounter['duplicate_resolution'] = self._duplicate_resolution(duplicates)
            if gifts is not None:
                state['rules']['gifts'] = gifts
            return _answer(True, 'Explicit rule choices recorded.', rules=dict(state['rules']))
        return self._change('rules', {'duplicates': duplicates, 'gifts': gifts}, evidence, apply)

    @staticmethod
    def _duplicate_resolution(rule):
        return {'ask': None, 'count_as_encounter': 'count', 'skip_owned_species': 'skip'}[rule]

    @staticmethod
    def _base_status(state, encounter):
        if encounter.get('dismissed'):
            return 'exempt', 'Dismissed unverified observation.'
        if not encounter['verified']:
            return 'pending', 'Encounter has not been independently verified.'
        kind = encounter['kind']
        if kind == 'starter':
            return 'eligible', 'Starter is separate from wild encounter areas.'
        if kind == 'gift':
            if state['rules']['gifts'] == 'ask':
                return 'pending', 'Gift eligibility needs an explicit gift rule.'
            return 'eligible', 'Separate gift rule applies.'
        phase = encounter['capture_rules_active']
        if phase is None:
            return 'pending', 'Whether Poké Balls had been obtained at this encounter is unverified.'
        if phase is False:
            return 'exempt', 'Encounter occurred before independently confirmed capture restrictions.'
        if encounter['duplicate']:
            resolution = encounter.get('duplicate_resolution')
            if resolution is None:
                return 'pending', 'Previously owned species: clarify whether the duplicate consumes or skips this encounter.'
            if resolution == 'skip':
                return 'exempt', 'Duplicate is skipped under the explicit rule.'
        return 'eligible', 'Eligible wild encounter.'

    @classmethod
    def _eligibility(cls, state, encounter):
        status, reason = cls._base_status(state, encounter)
        if status != 'eligible':
            return _answer(False, reason, legality=status, encounter_id=encounter['id'])
        for previous in sorted(state['encounters'].values(), key=lambda item: item['order']):
            if previous['order'] >= encounter['order']:
                break
            same_slot = (previous['kind'] == encounter['kind']
                         and (encounter['kind'] == 'starter' or previous['area_key'] == encounter['area_key']))
            if not same_slot:
                continue
            prior_status, _ = cls._base_status(state, previous)
            if prior_status == 'pending':
                return _answer(False, 'An earlier encounter in this slot has unresolved evidence or legality.', legality='pending', encounter_id=encounter['id'])
            if prior_status == 'eligible':
                return _answer(False, 'This encounter slot was already used; failed encounters also consume it.', legality='ineligible', encounter_id=encounter['id'])
        return _answer(True, reason, legality='eligible', encounter_id=encounter['id'])

    def record_encounter(self, area, species, *, evidence, encounter_id=None, kind='wild'):
        area, species = _text(area, 'Area'), _text(species, 'Species')
        if kind not in {'wild', 'gift', 'starter'}:
            raise ValueError('Encounter kind must be wild, gift, or starter.')
        encounter_id = _text(encounter_id or str(uuid.uuid4()), 'Encounter ID')
        payload = {'encounter_id': encounter_id, 'area': area, 'species': species, 'kind': kind}

        def apply(state, proof, event_id):
            existing = state['encounters'].get(encounter_id)
            if existing and (existing['area_key'], existing['species_key'], existing['kind']) != (_key(area, 'Area'), _key(species, 'Species'), kind):
                raise ValueError('An encounter ID cannot be reused for a different observation.')
            if existing and existing['verified']:
                return self._eligibility(state, existing)
            encounter = existing or {
                'id': encounter_id, 'area': area, 'area_key': _key(area, 'Area'),
                'species': species, 'species_key': _key(species, 'Species'), 'kind': kind,
                'order': event_id, 'status': 'observed', 'capture_rules_active': state['capture_rules_active'],
                'verified': False, 'duplicate': False, 'duplicate_resolution': None,
            }
            if proof['verified']:
                encounter['verified'] = True
                encounter['evidence_id'] = event_id
                encounter['duplicate'] = any(mon['species_key'] == encounter['species_key'] for mon in state['pokemon'].values())
                if encounter['duplicate']:
                    encounter['duplicate_resolution'] = self._duplicate_resolution(state['rules']['duplicates'])
            else:
                encounter['unverified_evidence_id'] = event_id
            state['encounters'][encounter_id] = encounter
            return self._eligibility(state, encounter)
        return self._change('encounter', payload, evidence, apply)

    def resolve_encounter(self, encounter_id, *, evidence, duplicate=None, capture_rules_active=None, dismiss=False):
        """Explicitly resolve a pending duplicate/phase or dismiss an unverified sighting.

        duplicate='skip' or 'count' applies only to a known duplicate. Phase is
        the verified phase WHEN that encounter occurred, not the current phase.
        """
        encounter_id = _text(encounter_id, 'Encounter ID')
        if duplicate not in (None, 'skip', 'count') or (capture_rules_active is not None and type(capture_rules_active) is not bool) or type(dismiss) is not bool:
            raise ValueError('Invalid encounter resolution.')

        def apply(state, proof, event_id):
            encounter = state['encounters'].get(encounter_id)
            if encounter is None:
                return _answer(False, 'Unknown encounter ID.')
            if not proof['verified'] or (duplicate is not None and proof['source'] not in {'user_report', 'operator_observation'}):
                return _answer(False, 'Encounter resolution requires independent evidence; duplicate choices require the user/operator.')
            if dismiss:
                if encounter['verified']:
                    return _answer(False, 'A verified encounter cannot be erased to recover a used area.')
                encounter['dismissed'] = True
            if duplicate is not None:
                if not encounter['duplicate'] or encounter['status'] != 'observed':
                    return _answer(False, 'Only an unresolved, observed duplicate can receive this ruling.')
                if encounter.get('duplicate_resolution') is not None and encounter['duplicate_resolution'] != duplicate:
                    return _answer(False, 'An established duplicate ruling cannot be reversed to recover an area.')
                encounter['duplicate_resolution'] = duplicate
            if capture_rules_active is not None:
                if encounter['capture_rules_active'] is not None and encounter['capture_rules_active'] != capture_rules_active:
                    return _answer(False, 'An established encounter phase cannot be reversed.')
                encounter['capture_rules_active'] = capture_rules_active
            encounter['resolution_evidence_id'] = event_id
            return self._eligibility(state, encounter)
        return self._change('encounter_resolution', {'encounter_id': encounter_id, 'duplicate': duplicate,
                            'capture_rules_active': capture_rules_active, 'dismiss': dismiss}, evidence, apply)

    def validate_catch(self, encounter_id=None, *, area=None, species=None):
        state = self.snapshot()
        if not encounter_id or encounter_id not in state['encounters']:
            return _answer(False, 'Current encounter identity is unverified; pause before throwing a ball.', legality='pending')
        encounter = state['encounters'][encounter_id]
        if encounter['kind'] != 'wild':
            return _answer(False, 'This is a gift/starter record, not a wild capture.', legality='ineligible')
        if area is None or species is None:
            return _answer(False, 'Current area and species must be confirmed for the proposed catch.', legality='pending')
        if (_key(area, 'Area'), _key(species, 'Species')) != (encounter['area_key'], encounter['species_key']):
            return _answer(False, 'Current area/species do not match the recorded encounter.', legality='pending')
        if encounter['status'] != 'observed':
            return _answer(False, 'This encounter has already ended; the area remains consumed if it was eligible.', legality='ineligible')
        return self._eligibility(state, encounter)

    def record_outcome(self, encounter_id, outcome, *, evidence, pokemon_id=None, nickname=None):
        encounter_id = _text(encounter_id, 'Encounter ID')
        if outcome not in {'caught', 'failed'}:
            raise ValueError('Outcome must be caught or failed (including KO, escape, or fleeing).')
        if outcome == 'caught':
            pokemon_id = _text(pokemon_id, 'Persistent Pokémon identity')
            nickname = _text(nickname, 'Nickname', 80) if nickname else None

        def apply(state, proof, event_id):
            encounter = state['encounters'].get(encounter_id)
            if not proof['verified'] or encounter is None or not encounter['verified']:
                return _answer(False, 'Outcome and encounter must both be independently verified.')
            if encounter['status'] != 'observed':
                if encounter['status'] == outcome and (outcome != 'caught' or encounter.get('pokemon_id') == pokemon_id):
                    return _answer(True, 'Outcome was already recorded; no duplicate ownership created.')
                return _answer(False, 'An ended encounter cannot be rewritten.')
            ruling = self._eligibility(state, encounter)
            if outcome == 'caught' and pokemon_id in state['pokemon']:
                return _answer(False, 'This Pokémon identity is already owned; verify the encounter identity.')
            encounter['status'] = outcome
            encounter['outcome_evidence_id'] = event_id
            if outcome == 'caught':
                encounter['pokemon_id'] = pokemon_id
                state['pokemon'][pokemon_id] = {
                    'id': pokemon_id, 'species': encounter['species'], 'species_key': encounter['species_key'],
                    'nickname': nickname, 'encounter_id': encounter_id, 'kind': encounter['kind'],
                    'legal': ruling['allowed'], 'legality': ruling['legality'], 'evidence_id': event_id,
                }
                if not ruling['allowed']:
                    state['violations'].append({'kind': 'capture_with_unresolved_or_ineligible_status',
                                                'pokemon_id': pokemon_id, 'encounter_id': encounter_id,
                                                'reason': ruling['reason'], 'evidence_id': event_id})
            return _answer(ruling['allowed'], 'Observed outcome recorded. ' + ruling['reason'],
                           encounter_id=encounter_id, outcome=outcome, legality=ruling['legality'])
        return self._change('outcome', {'encounter_id': encounter_id, 'outcome': outcome,
                            'pokemon_id': pokemon_id, 'nickname': nickname}, evidence, apply)

    def record_faint(self, pokemon_id, *, evidence, species=None):
        pokemon_id = _text(pokemon_id, 'Persistent Pokémon identity')
        if species is not None:
            species = _text(species, 'Species')

        def apply(state, proof, event_id):
            if not proof['verified']:
                return _answer(False, 'Unverified faint report saved for review; retirement has not been asserted.')
            state['retirements'].setdefault(pokemon_id, {'pokemon_id': pokemon_id, 'species': species,
                                                       'evidence_id': event_id, 'permanent': True})
            return _answer(False, 'Verified faint: permanently retire this Pokémon, including before Poké Balls.',
                           pokemon_id=pokemon_id, retired=True)
        return self._change('faint', {'pokemon_id': pokemon_id, 'species': species}, evidence, apply)

    def validate_use(self, pokemon_id):
        state = self.snapshot()
        if pokemon_id in state['retirements']:
            return _answer(False, 'This Pokémon is permanently retired after a verified faint.', retired=True)
        mon = state['pokemon'].get(pokemon_id)
        if mon is None:
            return _answer(False, 'Ownership/legality of this Pokémon is not verified.')
        if mon['legal'] is not True:
            return _answer(False, 'This Pokémon was caught with unresolved or ineligible status; pause for review.')
        return _answer(True, 'Verified legal Pokémon with no recorded faint.', retired=False)

    def summary(self):
        state = self.snapshot()
        spent, pending = set(), []
        for encounter in state['encounters'].values():
            base, reason = self._base_status(state, encounter)
            if encounter['kind'] == 'wild' and base == 'eligible':
                spent.add(encounter['area'])
            if base == 'pending':
                pending.append({'encounter_id': encounter['id'], 'area': encounter['area'], 'reason': reason})
        return {'run_id': self.run_id, 'capture_rules_active': state['capture_rules_active'],
                'rules': state['rules'], 'spent_wild_areas': sorted(spent),
                'owned': len(state['pokemon']), 'retired': len(state['retirements']),
                'pending': pending, 'violations': state['violations']}
