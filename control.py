#!/usr/bin/env python3
"""Local command-line companion for inspection and control; no remote calls."""
import argparse
import json
import time
import urllib.request

BASE = 'http://127.0.0.1:8765'


def call(route, data=None):
    request = urllib.request.Request(BASE + route, headers={'X-Pilot': '1', 'Content-Type': 'application/json'}, data=json.dumps(data).encode() if data is not None else None)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['state', 'auto', 'pause', 'takeover', 'press', 'guidance', 'observe', 'memory','navigate','route'])
    parser.add_argument('values', nargs='*')
    args = parser.parse_args()
    if args.command == 'state':
        result = call('/api/state')
        result['events'] = result['events'][-6:]
    elif args.command in ('auto', 'pause', 'takeover'):
        result = call('/api/control', {'action': args.command, 'limit': int(args.values[0]) if args.values else 0})
        result = {k: result[k] for k in ('mode', 'epoch', 'note')}
    elif args.command == 'press':
        result = call('/api/press', {'buttons': args.values[0].upper().split('+') if args.values[0] != 'WAIT' else [], 'frames': int(args.values[1]) if len(args.values) > 1 else 8})
        time.sleep(0.8 + (int(args.values[1]) if len(args.values) > 1 else 8) / 60)
        result = {k: result[k] for k in ('mode', 'epoch', 'note')}
    elif args.command == 'guidance':
        result = call('/api/guidance', {'text': ' '.join(args.values)})
        result = {'guidance': result['guidance'], 'mode': result['mode']}
    elif args.command == 'observe':
        result = call('/api/observe', json.loads(' '.join(args.values)))
        result = {'objective': result['objective'], 'location': result['location']}
    elif args.command == 'memory':
        result = call('/api/memory', {'address': int(args.values[0], 0), 'length': int(args.values[1], 0)})
    elif args.command == 'route':
        tile = None
        maps = [v for v in args.values if ',' not in v]
        spot = [v for v in args.values if ',' in v]
        if spot:
            x, y = spot[0].split(',')
            tile = {'x': int(x), 'y': int(y)}
        result = call('/api/route', {'itinerary': maps, 'tile': tile})
        result = {k: result[k] for k in ('mode', 'route')}
    elif args.command == 'navigate':
        result = call('/api/navigate',{'x':int(args.values[0]),'y':int(args.values[1]),'allow_warp':'warp' in args.values[2:]})
        result = {k:result[k] for k in ('mode','navigation_goal')}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
