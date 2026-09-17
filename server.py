#!/usr/bin/env python3
"""Local emulator control panel. Runs only on loopback; no external dependencies."""
from __future__ import annotations

import json
import os
import shutil
import socketserver
import threading
import time
import importlib
import hashlib
from learning import ExperienceMemory
from nuzlocke import NuzlockeLedger
from stall import StallMonitor
import route as router
from naming import NamingSession
import battle as battle_policy
from battle import BattleSession
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
RUNTIME.mkdir(exist_ok=True)
BUTTONS = {'A': 1, 'B': 2, 'SELECT': 4, 'START': 8, 'RIGHT': 16, 'LEFT': 32,
           'UP': 64, 'DOWN': 128, 'R': 256, 'L': 512}
lock = threading.RLock()
wakeup = threading.Event()
cancel = threading.Event()
connection = None
send_lock = threading.Lock()
memory_waiters = {}
experience = ExperienceMemory()
ledger = NuzlockeLedger(run_id=experience.active_run_id)
navigation_session = None
stall_monitor = StallMonitor()
route_session = None
naming_session = NamingSession()
battle_session = BattleSession()

state = {
    'connected': False, 'mode': 'paused', 'epoch': int(time.time() * 1000), 'frame': 0,
    'last_frame_at': 0, 'thinking': False, 'steps': 0, 'step_limit': 0,
    'objective': 'Prepare for and complete the first gym',
    'note': 'Connect the emulator, then start the AI demo.',
    'guidance': 'Complete the first gym battle in a Nuzlocke. Normal difficulty. Randomize Pokémon species only, not scaled species. Keep abilities and learnsets unrandomized. Choose a starter after inspecting the options. Train and prepare safely, then enter and complete the first gym when ready. Pause for deaths or unclear Nuzlocke legality. All gameplay AI must run locally.',
    'events': [], 'party': [], 'party_verified': False, 'badges': None,
    'location': 'Not observed yet', 'version': 'Radical Red 4.1',
    'randomizer': 'Species only — setup pending', 'randomizer_verified': False,
    'rules': ['First eligible encounter per area', 'Fainted Pokémon stay retired', 'No resetting to undo outcomes'],
    'deaths': [], 'encounters': [], 'ai_available': False, 'ai_error': None,
    'provider': 'Local AI', 'model': 'qwen3-vl:4b-instruct-q4_K_M',
    'telemetry_status': 'Awaiting live validation', 'learning': {}, 'run_id': experience.active_run_id,
    'world': None,
    'battle': None, 'emulation_speed': None,
    'navigation_goal': None, 'route': None, 'held_mask': 0,
}


def log(kind, text, source='controller'):
    entry = {'id': time.time_ns(), 'time': time.strftime('%H:%M:%S'), 'kind': kind,
             'text': str(text)[:1600], 'source': source}
    with lock:
        state['events'].append(entry)
        state['events'] = state['events'][-150:]
        with (RUNTIME / 'journal.jsonl').open('a') as stream:
            stream.write(json.dumps(entry) + '\n')
        temporary = RUNTIME / 'run-next.json'
        temporary.write_text(json.dumps(state))
        temporary.replace(RUNTIME / 'run.json')
    return entry


def send(line):
    with send_lock:
        peer = connection
        if peer is None:
            raise RuntimeError('The emulator is disconnected.')
        peer.sendall((line + '\n').encode('ascii'))


def switch_mode(mode, note=None):
    global cancel, navigation_session
    with lock:
        cancel.set()
        cancel = threading.Event()
        state['epoch'] += 1
        state['mode'] = mode
        state['thinking'] = False
        state['navigation_goal'] = None
        navigation_session = None
        stall_monitor.reset()
        epoch = state['epoch']
        if note:
            state['note'] = note
        try:
            send(f'RELEASE\t{epoch}')
        except (RuntimeError, OSError):
            pass
    wakeup.set()
    return epoch


def press(buttons, frames, epoch):
    if not isinstance(buttons, list) or any(b not in BUTTONS for b in buttons):
        raise ValueError('Unknown game button.')
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= 120:
        raise ValueError('A press must last 1–120 frames.')
    mask = 0
    for button in buttons:
        mask |= BUTTONS[button]
    if mask & 48 == 48 or mask & 192 == 192:
        raise ValueError('Opposite directions cannot be pressed together.')
    with lock:
        if epoch != state['epoch']:
            return False
        send(f'KEYS\t{mask}\t{frames}\t{epoch}')
    return True


def read_memory(address, length, timeout=2):
    request_id = str(time.time_ns())
    ready = threading.Event()
    holder = {}
    with lock:
        memory_waiters[request_id] = (ready, holder)
    try:
        send(f'READ\t{address}\t{length}\t{request_id}')
        if not ready.wait(timeout):
            raise TimeoutError('No memory response from emulator')
        return bytes.fromhex(holder['hex'])
    finally:
        with lock:
            memory_waiters.pop(request_id, None)


class Bridge(socketserver.StreamRequestHandler):
    def handle(self):
        global connection
        with lock:
            if connection is not None:
                self.request.sendall(b'ERROR\tOnly one emulator can connect\n')
                return
            connection = self.request
            state['connected'] = True
            state['note'] = 'Emulator connected. Ready for AI or manual control.'
        log('system', 'Emulator connected to the pilot.')
        speed_sample_time = time.monotonic()
        speed_sample_frame = None
        try:
            send(f'RELEASE\t{state["epoch"]}')
            for raw in self.rfile:
                fields = raw.decode('utf8', errors='replace').rstrip('\r\n').split('\t')
                if fields[0] == 'FRAME' and len(fields) >= 2:
                    with lock:
                        now = time.time()
                        sample_now = time.monotonic()
                        if speed_sample_frame is None:
                            speed_sample_frame = int(fields[1])
                            speed_sample_time = sample_now
                        elif sample_now-speed_sample_time>=1:
                            state['emulation_speed'] = round((int(fields[1])-speed_sample_frame)/(sample_now-speed_sample_time)/59.7275,1)
                            speed_sample_time,speed_sample_frame = sample_now,int(fields[1])
                        state['frame'] = int(fields[1])
                        state['last_frame_at'] = now
                        if len(fields) >= 3:
                            state['epoch'] = max(state['epoch'], int(fields[2]))
                        # The emulator echoes the key mask it is actually holding,
                        # which is the only way to tell a dropped press from an
                        # ignored one.
                        if len(fields) >= 4:
                            state['held_mask'] = int(fields[3])
                elif fields[0] == 'PARTY_ZERO' and len(fields) == 3:
                    from telemetry import decode_party, ROM_SHA256
                    try:
                        certificate = json.loads((RUNTIME/'telemetry-validated.json').read_text())
                    except (OSError, ValueError):
                        certificate = {}
                    verified = certificate.get('validated') is True and certificate.get('rom_sha256') == ROM_SHA256
                    snapshot = decode_party(int(fields[1]),fields[2],RUNTIME/'radical-red-demo.gba',validated=verified)
                    if snapshot.get('eligible_for_events'):
                        for mon in snapshot['party']:
                            if mon['hp']==0 and not mon.get('is_egg'):
                                pause_for_faint({'identity':mon['identity'],'species':mon['species'],
                                    'detail':mon['species']+' fainted; recorded on the emulated frame at zero HP.'})
                elif fields[0] == 'DATA' and len(fields) >= 3:
                    with lock:
                        waiter = memory_waiters.get(fields[1])
                        if waiter:
                            waiter[1]['hex'] = fields[2]
                            waiter[0].set()
                elif fields[0] == 'ERROR':
                    log('error', 'Emulator: ' + ' '.join(fields[1:]))
                elif fields[0] == 'MANUAL':
                    switch_mode('manual', 'You have control.')
        except (OSError, ValueError) as exc:
            log('error', 'Emulator connection ended: ' + str(exc))
        finally:
            with lock:
                connection = None
                state['connected'] = False
            switch_mode('paused', 'Emulator disconnected. AI is paused.')
            log('system', 'Emulator disconnected; AI input stopped.')


def ai_loop():
    global navigation_session, route_session
    while True:
        wakeup.wait(0.5)
        wakeup.clear()
        with lock:
            if state['mode'] != 'auto' or not state['connected'] or state['thinking']:
                continue
            if state['step_limit'] and state['steps'] >= state['step_limit']:
                switch_mode('paused', 'Demo decision limit reached. Resume whenever you want.')
                log('system', 'Demo paused at its decision limit.')
                continue
            if not (RUNTIME / 'frame.png').exists() or time.time() - state['last_frame_at'] > 5:
                continue
            epoch = state['epoch']
            token = cancel
            state['thinking'] = True
            context = {key: state[key] for key in ['location', 'party', 'party_verified', 'badges', 'deaths', 'encounters', 'randomizer', 'rules', 'run_id']}
            context['goal'] = 'Prepare for and defeat Brock, then stop after the first badge.'
            context['recent_events'] = state['events'][-16:]
            context['step'] = state['steps'] + 1
            if state.get('world') and state['world'].get('validated'):
                context['world'] = state['world']
            if state.get('battle') and state['battle'].get('validated'):
                context['battle'] = state['battle']
            context['nuzlocke'] = ledger.summary()
            guidance = state['guidance']
        try:
            import local_driver
            importlib.reload(local_driver)
            shot = RUNTIME / f'observation-{epoch}.png'
            shutil.copyfile(RUNTIME / 'frame.png', shot)
            before_hash = hashlib.sha256(shot.read_bytes()).hexdigest()
            context['image_hash'] = before_hash
            context['experience'] = experience.retrieve_lessons(context)
            context['stall'] = stall_monitor.status()
            from screen_reader import read_screen
            from dialogue import choose_dialogue_action
            screen = read_screen(shot)
            if screen.get('valid'):
                context['screen_text'] = screen['text']
                context['dialogue_continue_arrow_visible'] = screen.get('continue_indicator',{}).get('visible',False)
                context['ocr_warning'] = screen.get('warning')
            import naming
            importlib.reload(naming)
            action = naming_session.choose(screen)
            decision_source = 'Local naming policy' if action else None
            with lock:
                live_battle = state.get('battle')
            if action is None and live_battle and live_battle.get('in_battle'):
                try:
                    action = battle_session.choose(live_battle, screen)
                except (ValueError, KeyError, TypeError) as exc:
                    action = {'buttons': [], 'frames': 1, 'pause': True, 'events': [],
                              'objective': 'Paused in battle',
                              'note': 'Battle could not be scored: %s' % exc}
                if action is not None:
                    decision_source = 'Local battle policy'
            with lock:
                walking_goal = state.get('navigation_goal') if action is None else None
            if walking_goal:
                from navigation import NavigationSession, choose_navigation_action
                world = json.loads((RUNTIME/'world-snapshot.json').read_text())
                from dialogue import dialogue_is_active, menu_is_open
                world['dialogue_active'] = dialogue_is_active(screen)
                world['menu_active'] = menu_is_open(screen)
                if navigation_session is None:
                    navigation_session = NavigationSession()
                navigation = choose_navigation_action(world,walking_goal,state=navigation_session,cancel=token,epoch=epoch)
                if navigation['status']=='step':
                    action = navigation['action']
                    decision_source = 'Local navigator'
                elif navigation['status']=='waiting':
                    action = {'buttons':[],'frames':3,'objective':state['objective'],'note':navigation['reason'],'events':[],'pause':False}
                    decision_source = 'Local navigator'
                else:
                    with lock:
                        state['navigation_goal'] = None
                    navigation_session = None
                    log('navigation',navigation['reason'],'Local navigator')
            from dialogue import dialogue_is_active as _dialogue_up
            if action is None and not _dialogue_up(screen):
                with lock:
                    routing = route_session
                    mid_battle = bool((state.get('battle') or {}).get('in_battle'))
                if routing is not None and not mid_battle:
                    world_now = json.loads((RUNTIME/'world-snapshot.json').read_text())
                    outcome = routing.choose(world_now, router.read_connections(read_memory))
                    decision_source = 'Local router'
                    if outcome['status'] == 'walk':
                        target = outcome['target']
                        proof = json.loads((RUNTIME/'world-validated.json').read_text())
                        with lock:
                            state['navigation_goal'] = {'map_id': world_now['map_id'], 'target': target,
                                'step_frames': proof['step_frames'], 'calibration_validated': True,
                                'overworld_confirmed': True, 'avoid_encounters': True,
                                'allow_target_warp': bool(outcome.get('allow_warp'))}
                        navigation_session = None
                        log('navigation', outcome['reason'], 'Local router')
                        action = {'buttons': [], 'frames': 1, 'objective': state['objective'],
                                  'note': outcome['reason'], 'events': [], 'pause': False}
                    elif outcome['status'] == 'retry':
                        log('navigation', outcome['reason'], 'Local router')
                        action = {'buttons': [], 'frames': 1, 'objective': state['objective'],
                                  'note': outcome['reason'], 'events': [], 'pause': False}
                    elif outcome['status'] == 'cross':
                        action = {'buttons': [outcome['button']], 'frames': outcome['frames'],
                                  'objective': state['objective'], 'note': outcome['reason'],
                                  'events': [], 'pause': False}
                    else:
                        with lock:
                            state['route'] = None
                            route_session = None
                        log('navigation', outcome['reason'], 'Local router')
                        if outcome['status'] == 'paused':
                            action = {'buttons': [], 'frames': 1, 'objective': state['objective'],
                                      'note': outcome['reason'], 'events': [], 'pause': True}
            if action is None:
                action = choose_dialogue_action(screen,context)
            decision_source = decision_source or ('Local dialogue policy' if action else 'Local vision model')
            if action is None:
                action = local_driver.choose_action(str(shot), context, guidance, token)
            with lock:
                if state['mode'] != 'auto' or state['epoch'] != epoch or token.is_set():
                    continue
                # The model is told to keep making progress; this enforces it.
                review = stall_monitor.review(action, image_hash=before_hash)
                if review['intervention']:
                    log('system', review['reason'], 'Stall watchdog')
                    action, decision_source = review['action'], 'Stall watchdog'
                if action['buttons']:
                    # Async socket arrival can precede a frame boundary without
                    # a game key poll; three frames gives one reliable menu tap.
                    action['frames'] = max(3,action['frames'])
                state['steps'] += 1
                state['thinking'] = False
                state['objective'] = action['objective']
                state['note'] = action['note']
                log('decision', action['note'], decision_source)
                for event in action.get('events', []):
                    if isinstance(event, dict):
                        log('observation', str(event.get('detail', event)), 'AI observation')
                    else:
                        log('observation', str(event), 'AI observation')
                if action.get('pause'):
                    switch_mode('paused', action['note'])
                    continue
                buttons = action.get('buttons', [])
                frames = action.get('frames', 1)
                press(buttons, frames, epoch)
                log('input', (' + '.join(buttons) or 'Wait') + f' · {frames} frames', 'AI')
            token.wait(max(1.0, frames / 60 + 0.65))
            with lock:
                if state['epoch'] == epoch and not token.is_set():
                    after_context = {key: state[key] for key in ['objective', 'location', 'party', 'party_verified', 'badges', 'run_id']}
                    after_hash = hashlib.sha256((RUNTIME / 'frame.png').read_bytes()).hexdigest()
                    experience.record_transition(context, action, after_context, before_hash, after_hash)
                    state['learning'] = experience.summarize()
        except Exception as exc:
            with lock:
                if state['epoch'] == epoch and state['mode'] == 'auto':
                    switch_mode('paused', 'AI paused: ' + str(exc)[:400])
                    state['ai_error'] = str(exc)[:400]
                    log('error', 'AI paused: ' + str(exc)[:600])
        finally:
            with lock:
                if state['epoch'] == epoch:
                    state['thinking'] = False


def public_state():
    with lock:
        snapshot = json.loads(json.dumps(state))
    snapshot['frame_available'] = (RUNTIME / 'frame.png').exists()
    snapshot['frame_age'] = time.time() - snapshot['last_frame_at'] if snapshot['last_frame_at'] else None
    snapshot['ai_available'] = (ROOT / 'local_driver.py').exists()
    snapshot['nuzlocke'] = ledger.summary()
    return snapshot


class HTTP(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data, mime='application/json'):
        if mime == 'application/json':
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        route = self.path.split('?')[0]
        if route == '/api/state':
            return self.reply(200, public_state())
        if route in ('/', '/index.html'):
            return self.reply(200, (ROOT / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if route == '/frame.png' and (RUNTIME / 'frame.png').exists():
            return self.reply(200, (RUNTIME / 'frame.png').read_bytes(), 'image/png')
        return self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:8765', 'http://localhost:8765'):
                return self.reply(403, {'error': 'Local panel requests only.'})
            if self.headers.get('X-Pilot') != '1':
                return self.reply(403, {'error': 'Missing control header.'})
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 16000:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(size))
            route = self.path
            if route == '/api/control':
                action = data.get('action')
                if action == 'auto':
                    with lock:
                        if not state['connected']:
                            raise ValueError('Connect the emulator first.')
                        limit = int(data.get('limit', 0))
                        if not 0 <= limit <= 10000:
                            raise ValueError('Choose 0 for continuous play, or 1–10,000 AI decisions.')
                        state['steps'] = 0
                        state['step_limit'] = limit
                        state['ai_error'] = None
                    switch_mode('auto', 'AI demo starting…')
                    log('system', f'Local AI enabled for up to {limit} decisions.' if limit else 'Local AI enabled for continuous play. No Codex credits are used.')
                elif action in ('pause', 'takeover'):
                    switch_mode('manual' if action == 'takeover' else 'paused', 'You have control.' if action == 'takeover' else 'AI paused.')
                    log('system', 'You took control.' if action == 'takeover' else 'AI paused.')
                elif action == 'release':
                    with lock:
                        if state['mode'] != 'manual':
                            raise ValueError('Manual control is not active.')
                        send(f'RELEASE\t{state["epoch"]}')
                else:
                    raise ValueError('Unknown control action.')
            elif route == '/api/press':
                with lock:
                    if state['mode'] != 'manual':
                        switch_mode('manual', 'You have control.')
                    epoch = state['epoch']
                press(data.get('buttons', []), data.get('frames', 8), epoch)
            elif route == '/api/guidance':
                guidance = str(data.get('text', '')).strip()
                if not guidance or len(guidance) > 3000:
                    raise ValueError('Enter an instruction of up to 3,000 characters.')
                with lock:
                    state['guidance'] = guidance
                    was_auto = state['mode'] == 'auto'
                    switch_mode('auto' if was_auto else state['mode'], 'Instruction saved. It applies to the next decision.')
                log('instruction', guidance, 'you')
                experience.record_correction(guidance, {'run_id': state['run_id'], 'location': state['location']})
            elif route == '/api/observe':
                # Trusted local operator records only facts seen in the emulator.
                allowed = {'location', 'badges', 'party', 'party_verified', 'randomizer', 'randomizer_verified', 'objective', 'note'}
                with lock:
                    for key in allowed:
                        if key in data:
                            state[key] = data[key]
                if data.get('event'):
                    log('observation', str(data['event']), 'verified by operator')
            elif route == '/api/route':
                global route_session
                with lock:
                    world = state.get('world') or {}
                    if not world.get('validated') or world.get('in_battle'):
                        raise ValueError('A validated overworld is required.')
                    itinerary = data.get('itinerary')
                    if not isinstance(itinerary, list) or not itinerary:
                        raise ValueError('A route needs an itinerary of map ids.')
                    if itinerary[0] != world['map_id']:
                        raise ValueError('The itinerary must start on the current map %s.' % world['map_id'])
                    tile = data.get('tile')
                    session = router.RouteSession(
                        [str(item) for item in itinerary],
                        final_tile=(int(tile['x']), int(tile['y'])) if tile else None)
                    route_session = session
                    state['route'] = {'itinerary': session.itinerary,
                                      'final_tile': list(session.final_tile) if session.final_tile else None}
                    state['navigation_goal'] = None
                    state['step_limit'] = 0
                    switch_mode('auto', 'Following the planned route to ' + session.itinerary[-1] + '.')
                log('navigation', 'Route planned: ' + ' -> '.join(session.itinerary), 'Local router')
            elif route == '/api/navigate':
                proof = json.loads((RUNTIME/'world-validated.json').read_text())
                with lock:
                    world = state.get('world') or {}
                    if not world.get('validated') or world.get('in_battle'):
                        raise ValueError('A validated overworld is required.')
                    x,y = int(data['x']),int(data['y'])
                    if not 0<=x<world['width'] or not 0<=y<world['height']:
                        raise ValueError('Target outside current map.')
                    switch_mode('auto','Walking to the observed destination.')
                    state['navigation_goal'] = {'map_id':world['map_id'],'target':{'x':x,'y':y},'step_frames':proof['step_frames'],
                        'calibration_validated':True,'overworld_confirmed':True,'avoid_encounters':True,
                        'allow_target_warp':bool(data.get('allow_warp'))}
                    state['step_limit'] = 0
                log('navigation',f'Navigate to ({x}, {y}) on map {world["map_id"]}.','Local navigator')
            elif route == '/api/memory':
                address, length = int(data['address']), int(data['length'])
                if not 1 <= length <= 1024:
                    raise ValueError('Read size must be 1–1024 bytes.')
                if not (0x02000000 <= address <= 0x02040000 - length or 0x03000000 <= address <= 0x03008000 - length or 0x08000000 <= address <= 0x0A000000 - length):
                    raise ValueError('Address outside read-only game regions.')
                return self.reply(200, {'hex': read_memory(address, length).hex()})
            else:
                return self.reply(404, {'error': 'Not found'})
            return self.reply(200, public_state())
        except (ValueError, TypeError, KeyError, RuntimeError, TimeoutError, OSError) as exc:
            return self.reply(400, {'error': str(exc)})


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def publish_telemetry(snapshot):
    with lock:
        state['telemetry_status'] = snapshot.get('status', 'unavailable')
        if snapshot.get('valid') and snapshot.get('validated'):
            from battle_data import enrich_party as label_moves
            from combat import enrich_party
            previous = [(m.get('identity'), m.get('level')) for m in state['party']]
            rom = RUNTIME / 'radical-red-demo.gba'
            state['party'] = enrich_party(label_moves(snapshot['party'],rom),rom)
            state['party_verified'] = True
            current = [(m.get('identity'), m.get('level')) for m in state['party']]
            if current != previous:
                log('party', 'Current party: ' + ', '.join(f'{m["species"]} Lv. {m["level"]}' for m in snapshot['party']), 'verified game memory')
        else:
            state['party_verified'] = False
    temporary = RUNTIME / 'telemetry-next.json'
    temporary.write_text(json.dumps(snapshot))
    temporary.replace(RUNTIME / 'telemetry-snapshot.json')


def pause_for_faint(event):
    with lock:
        ledger.record_faint(event['identity'],evidence={'source':'validated_game_memory','verified':True,'detail':event['detail']})
        switch_mode('paused', event['detail'])
        if not any(item.get('identity') == event['identity'] for item in state['deaths']):
            state['deaths'].append(event)
            log('death', event['detail'], 'verified game memory')


def world_loop():
    from world import observe_world
    from combat import observe_battle
    while True:
        try:
            with lock:
                connected = state['connected']
            if connected:
                validated = (RUNTIME / 'world-validated.json').exists()
                observed = observe_world(read_memory, validated=validated)
                with lock:
                    observed['frame'] = state['frame']
                if observed.get('valid'):
                    compact = {key: observed.get(key) for key in ('valid', 'validated', 'position', 'map_id', 'region_id', 'in_battle', 'width', 'height', 'facing', 'position_sources_agree', 'avatar_candidate', 'warps')}
                    with lock:
                        state['world'] = compact
                temporary = RUNTIME / 'world-next.json'
                temporary.write_text(json.dumps(observed))
                temporary.replace(RUNTIME / 'world-snapshot.json')
                battle = observe_battle(read_memory,RUNTIME/'radical-red-demo.gba',validated=(RUNTIME/'battle-validated.json').exists())
                (RUNTIME/'battle-snapshot.json').write_text(json.dumps(battle))
                with lock:
                    state['battle'] = battle
        except Exception as exc:
            with lock:
                state['world'] = {'valid': False, 'validated': False, 'error': str(exc)[:200]}
        time.sleep(2)


def main():
    saved = RUNTIME / 'run.json'
    if saved.exists():
        try:
            previous = json.loads(saved.read_text())
            for key in ('objective', 'guidance', 'events', 'party', 'party_verified', 'badges', 'location', 'randomizer', 'randomizer_verified', 'deaths', 'encounters'):
                if key in previous:
                    state[key] = previous[key]
            state['epoch'] = max(state['epoch'], previous.get('epoch', 0) + 1)
        except (OSError, ValueError):
            pass
    tcp = TCPServer(('127.0.0.1', 8766), Bridge)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    threading.Thread(target=ai_loop, daemon=True).start()
    from monitor import monitor
    threading.Thread(target=monitor, args=(read_memory, publish_telemetry, pause_for_faint), daemon=True).start()
    threading.Thread(target=world_loop, daemon=True).start()
    log('system', 'Local pilot started. Continuing the current run.')
    server = ThreadingHTTPServer(('127.0.0.1', 8765), HTTP)
    print('Radical Red pilot: http://127.0.0.1:8765', flush=True)
    try:
        server.serve_forever()
    finally:
        switch_mode('paused')
        tcp.shutdown()


if __name__ == '__main__':
    main()
