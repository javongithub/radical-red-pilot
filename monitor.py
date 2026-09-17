"""Continuous read-only party monitor; run ``monitor`` in a daemon thread.

Callbacks: read_memory(address, length)->bytes, publish_callback(snapshot),
pause_callback(event). Callback execution is synchronous; keep callbacks short.
Only the operator may create runtime/telemetry-validated.json after independently
comparing decoded values with the visible game. This module never writes it.
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

from telemetry import PARTY_ADDRESS, PARTY_COUNT_ADDRESS, ROM_SHA256, decode_party

ROOT = Path(__file__).resolve().parent


class PartyMonitor:
    def __init__(self, read_memory, publish_callback, pause_callback, *, runtime=None, rom_path=None):
        self.read_memory = read_memory
        self.publish = publish_callback
        self.pause = pause_callback
        self.runtime = Path(runtime) if runtime else ROOT / "runtime"
        self.rom_path = Path(rom_path) if rom_path else self.runtime / "radical-red-demo.gba"
        self.previous = {}
        self.reported = set()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.event_path = self.runtime / "telemetry-events.jsonl"
        if self.event_path.exists():
            for line in self.event_path.read_text().splitlines():
                try:
                    event = json.loads(line)
                    if event.get("type") == "faint_observed" and event.get("rom_sha256") == ROM_SHA256:
                        self.reported.add(event["identity"])
                except (ValueError, KeyError):
                    continue

    def validation_enabled(self):
        try:
            marker = json.loads((self.runtime / "telemetry-validated.json").read_text())
            return marker.get("validated") is True and marker.get("rom_sha256") == ROM_SHA256
        except (OSError, ValueError, AttributeError):
            return False

    def poll_once(self):
        """Read one snapshot. Read failures clear the transition baseline."""
        validated = self.validation_enabled()
        try:
            before = self.read_memory(PARTY_COUNT_ADDRESS, 1)
            raw = self.read_memory(PARTY_ADDRESS, 600)
            after = self.read_memory(PARTY_COUNT_ADDRESS, 1)
            if before != after:
                raise ValueError("Party changed during memory read; waiting for a stable snapshot.")
            snapshot = decode_party(before, raw, self.rom_path, validated=validated)
        except Exception as exc:
            snapshot = {"valid": False, "validated": validated, "eligible_for_events": False,
                        "status": "unavailable", "party": [], "errors": [str(exc)[:400]],
                        "source": "read-only emulator memory"}
        snapshot["observed_at"] = time.time()
        snapshot["events"] = []
        if snapshot.get("eligible_for_events"):
            current = {mon["identity"]: mon for mon in snapshot["party"]}
            for identity, mon in current.items():
                previous = self.previous.get(identity)
                if (previous and previous["hp"] > 0 and mon["hp"] == 0
                        and not mon.get("is_egg") and identity not in self.reported):
                    event = {
                        "type": "faint_observed", "time": time.time(),
                        "rom_sha256": ROM_SHA256, "identity": identity,
                        "species": mon["species"], "nickname": mon["nickname"],
                        "level": mon["level"], "previous_hp": previous["hp"], "hp": 0,
                        "max_hp": mon["max_hp"], "source": "validated party telemetry",
                        "detail": f"{mon['nickname'] or mon['species']} changed from {previous['hp']} HP to 0 HP. AI paused for review.",
                    }
                    # Stop input before publishing or recording. A callback failure
                    # propagates to the outer loop and is retried on the next poll.
                    self.pause(event)
                    with self.event_path.open("a") as stream:
                        stream.write(json.dumps(event) + "\n")
                    self.reported.add(identity)
                    snapshot["events"].append(event)
            self.previous = current
        else:
            self.previous = {}
        self.publish(snapshot)
        return snapshot


def monitor(read_memory, publish_callback, pause_callback, *, runtime=None, rom_path=None,
            interval=1.0, stop_event=None):
    """Blocking daemon-thread target. Stops when the optional Event is set.

    This polls in manual and automatic modes alike. No new-member event is
    generated: gifts, captures, and PC withdrawals require separate observations.
    Missing members are never treated as deaths. Previously zero-HP members at
    monitor startup are reported in snapshots without inventing a transition.
    """
    watcher = PartyMonitor(read_memory, publish_callback, pause_callback,
                           runtime=runtime, rom_path=rom_path)
    stopped = stop_event if stop_event is not None else threading.Event()
    interval = max(0.2, float(interval))
    while not stopped.is_set():
        started = time.monotonic()
        try:
            watcher.poll_once()
        except Exception as exc:
            # Keep the monitor alive through transient callback failures.
            try:
                publish_callback({"valid": False, "validated": watcher.validation_enabled(),
                                  "eligible_for_events": False, "status": "monitor_error",
                                  "party": [], "errors": [str(exc)[:400]], "events": [],
                                  "observed_at": time.time(), "source": "party monitor"})
            except Exception:
                pass
        stopped.wait(max(0, interval - (time.monotonic() - started)))
