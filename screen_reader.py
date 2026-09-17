"""Offline Apple Vision OCR for emulator screenshots.

No game actions, network, or model service. The bundled Swift helper uses Apple
Vision on this Mac. Results are observations, not authoritative game state.
"""
from __future__ import annotations

import atexit
from collections import OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / 'screen-reader'
_lock = threading.Lock()
_process: subprocess.Popen | None = None
_cache: OrderedDict[str, dict] = OrderedDict()


def _stop() -> None:
    global _process
    if _process is not None:
        try:
            _process.kill()
            _process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _process = None


atexit.register(_stop)


def available() -> bool:
    return HELPER.is_file()


def read_screen(image_path: str | Path, *, timeout: float = 8.0) -> dict:
    """Return visible text and pixel evidence within a bounded wait.

    Boxes use original-image pixel coordinates with the origin at the top left.
    OCR confidence is Vision's recognition score, not a correctness guarantee.
    An exact red-triangle match is visual evidence only; absence may be blinking.
    Errors return valid=False and never cause a game action.
    """
    global _process
    start = time.monotonic()
    timeout = max(0.1, min(float(timeout), 30.0))
    result = {'valid': False, 'provider': 'Apple Vision (local)', 'text': '',
              'lines': [], 'image_sha256': None,
              'observed_at': datetime.now(timezone.utc).isoformat()}
    try:
        data = Path(image_path).read_bytes()
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 10_000_000:
            raise ValueError('Expected a PNG screenshot no larger than 10 MB')
        digest = hashlib.sha256(data).hexdigest()
        result['image_sha256'] = digest
        if not _lock.acquire(timeout=timeout):
            raise TimeoutError('OCR worker is busy')
        try:
            if digest in _cache:
                result.update(json.loads(json.dumps(_cache[digest])))
                result['cached'] = True
                _cache.move_to_end(digest)
            else:
                if not HELPER.is_file():
                    raise RuntimeError('Local OCR helper has not been compiled')
                if _process is None or _process.poll() is not None:
                    _process = subprocess.Popen([str(HELPER), '--stdio'],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0)
                with tempfile.TemporaryDirectory(prefix='radical-red-ocr-') as temp:
                    snapshot = Path(temp) / 'frame.png'
                    snapshot.write_bytes(data)
                    _process.stdin.write((json.dumps({'path': str(snapshot)}) + '\n').encode())
                    _process.stdin.flush()
                    with selectors.DefaultSelector() as selector:
                        selector.register(_process.stdout, selectors.EVENT_READ)
                        line = bytearray()
                        while not line.endswith(b'\n'):
                            remaining = timeout - (time.monotonic() - start)
                            if remaining <= 0 or not selector.select(remaining):
                                _stop()
                                raise TimeoutError('Local OCR exceeded its time limit')
                            chunk = os.read(_process.stdout.fileno(), 16_384)
                            if not chunk or len(line) + len(chunk) > 128_000:
                                _stop()
                                raise RuntimeError('Local OCR helper returned incomplete output')
                            line.extend(chunk)
                        observation = json.loads(line)
                result.update(observation)
                result['cached'] = False
                if result.get('valid'):
                    _cache[digest] = observation
                    while len(_cache) > 16:
                        _cache.popitem(last=False)
        finally:
            _lock.release()
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        result['error'] = str(exc)
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000, 1)
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--timeout', type=float, default=8)
    args = parser.parse_args()
    print(json.dumps(read_screen(args.image, timeout=args.timeout), ensure_ascii=False, indent=2))
