"""Local closed-loop walking decisions; this module never presses a button.

Integration:
    session = NavigationSession()
    goal = {"map_id": "4.3", "target": {"x": 5, "y": 4},
            "step_frames": 16, "calibration_validated": True,
            "overworld_confirmed": True, "avoid_encounters": True}
    # Supply a NEW coherent world.py snapshot with the emulator frame attached.
    decision = choose_navigation_action(snapshot, goal, state=session,
                                        cancel=cancel_event, epoch=control_epoch)
    # Send decision["action"] only when status == "step", after checking the
    # same cancellation token and epoch again under the server's input lock.

Use a new session after a manual takeover, changed goal, or pause. "arrived" means
the requested coordinate was observed on the same map; ask vision for the next
goal. "waiting" emits no input. The caller must keep checking battle/death rules
and must never execute the returned full path as an open-loop macro.

Validation of the map decoder and walking calibration are supplied by the caller;
this code does not infer validation from successful-looking memory values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any

from world import MAX_CELLS, plan_path

_DIRECTIONS = {"UP": (0, -1), "RIGHT": (1, 0), "DOWN": (0, 1), "LEFT": (-1, 0)}


def _point(value):
    if isinstance(value, dict):
        value = value.get("x"), value.get("y")
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("Expected an x/y coordinate")
    if any(isinstance(n, bool) or not isinstance(n, int) for n in value):
        raise ValueError("Coordinates must be integers")
    return tuple(value)


def _integer(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _cancelled(token):
    return bool(token is not None and token.is_set())


@dataclass
class NavigationSession:
    """One walking goal under one control epoch; all mutations are synchronized."""

    epoch: int | None = None
    signature: tuple | None = None
    pending: dict | None = None
    last_frame: int | None = None
    successful_steps: int = 0
    failed_steps: int = 0
    consecutive_failures: int = 0
    blocked_tiles: set = field(default_factory=set)
    stopped_reason: str | None = None
    stop_status: str = "paused"
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def cancel(self):
        self._cancel.set()

    def _stop(self, status, reason, **details):
        self.stopped_reason, self.stop_status = reason, status
        self.pending = None
        return self._decision(status, reason, **details)

    def _decision(self, status, reason, **details):
        return {"status": status, "action": None, "reason": reason, "epoch": self.epoch,
                "successful_steps": self.successful_steps, "failed_steps": self.failed_steps,
                **details}

    def choose(self, world, goal, *, cancel=None, epoch=None):
        with self._lock:
            if self._cancel.is_set() or _cancelled(cancel):
                return self._stop("cancelled", "Control was cancelled; no navigation input was issued.")
            if self.stopped_reason:
                return self._decision(self.stop_status, self.stopped_reason)
            try:
                return self._choose(world, goal, cancel=cancel, epoch=epoch)
            except (ValueError, KeyError, TypeError, IndexError) as error:
                return self._stop("paused", "Navigation data is incomplete or unsafe: " + str(error))

    def _choose(self, world, goal, *, cancel, epoch):
        if not isinstance(world, dict) or not isinstance(goal, dict):
            raise ValueError("World and goal must be objects")
        current_epoch = _integer(epoch if epoch is not None else goal.get("epoch"), 0, 2**53 - 1, "epoch")
        if self.epoch is not None and current_epoch != self.epoch:
            return self._stop("cancelled", "Control epoch changed; create a new navigation session.")
        self.epoch = current_epoch
        if world.get("valid") is not True or world.get("validated") is not True:
            return self._stop("paused", "The current world snapshot has not been validated.")
        if world.get("in_battle") is not False:
            return self._stop("paused", "Battle state requires the battle controller or visual inspection.")
        if world.get("dialogue_active") or world.get("menu_active"):
            return self._stop("paused", "Dialogue or a menu interrupted navigation.")
        if goal.get("overworld_confirmed") is not True:
            return self._stop("paused", "Confirm the overworld visually before starting a walking goal.")
        if goal.get("calibration_validated") is not True:
            return self._stop("paused", "Walking frame calibration has not been validated.")
        step_frames = _integer(goal.get("step_frames"), 1, 24, "step_frames")
        settle_frames = _integer(goal.get("settle_frames", 6), 2, 30, "settle_frames")
        max_steps = _integer(goal.get("max_steps", 20), 1, 100, "max_steps")
        max_failures = _integer(goal.get("max_failures", 3), 1, 5, "max_failures")
        max_pending = _integer(goal.get("max_pending_frames", 120), step_frames + settle_frames, 240, "max_pending_frames")
        frame = _integer(world.get("frame"), 0, 2**32 - 1, "world.frame")
        target, position = _point(goal.get("target")), _point(world.get("position"))
        goal_map = str(goal.get("map_id", ""))
        if not goal_map or str(world.get("map_id")) != goal_map:
            return self._stop("paused", "The map changed; visual inspection is required before continuing.")
        signature = goal_map, target, step_frames, settle_frames, bool(goal.get("avoid_encounters", True))
        if self.signature is not None and signature != self.signature:
            return self._stop("paused", "The walking goal changed; create a new navigation session.")
        self.signature = signature
        if self.last_frame is not None and frame < self.last_frame:
            return self._stop("paused", "The emulator frame moved backwards; navigation stopped.")
        self.last_frame = frame
        avatar = world.get("avatar_candidate")
        if not isinstance(avatar, dict):
            raise ValueError("Missing player movement state")
        if avatar.get("prevent_step") is not False:
            return self._stop("paused", "The game is preventing movement; inspect the current interaction.")
        running = _integer(avatar.get("running_state"), 0, 2, "running_state")
        transition = _integer(avatar.get("tile_transition_state"), 0, 2, "tile_transition_state")
        stable = world.get("position_sources_agree") is True and running == 0 and transition == 0

        if self.pending is not None:
            pending = self.pending
            elapsed = frame - pending["frame"]
            if elapsed < step_frames + settle_frames:
                return self._decision("waiting", "Waiting for the bounded button press and movement animation.")
            if not stable:
                if elapsed <= max_pending:
                    return self._decision("waiting", "Waiting for a stable player coordinate.")
                return self._stop("paused", "Player movement did not settle within the allowed frame budget.")
            self.pending = None
            if position == pending["expected"]:
                self.successful_steps += 1
                self.consecutive_failures = 0
            elif position == pending["start"]:
                direction_code={"DOWN":1,"UP":2,"LEFT":3,"RIGHT":4}[pending['button']]
                turned=(pending.get('facing')!=direction_code and world.get('facing')==direction_code)
                if not turned:
                    self.failed_steps += 1
                    self.consecutive_failures += 1
                    self.blocked_tiles.add(pending["expected"])
                    if self.consecutive_failures >= max_failures:
                        return self._stop("paused", "Repeated walking attempts made no progress; inspect the obstruction.")
            else:
                return self._stop("paused", "Movement differed from one calibrated tile; inspect position and calibration.",
                                  expected=list(pending["expected"]), observed=list(position))
        elif not stable:
            return self._stop("paused", "Player position or movement state is uncertain before navigation.")

        if position == target:
            return self._stop("arrived", "The requested coordinate was observed on the current map.", position=list(position))
        if self.successful_steps >= max_steps:
            return self._stop("paused", "Walking checkpoint reached; request a fresh visual decision.")
        grid = world.get("grid")
        if not isinstance(grid, dict) or grid.get("validated") is not True:
            raise ValueError("Missing validated collision grid")
        width = _integer(grid.get("width"), 1, 256, "grid.width")
        height = _integer(grid.get("height"), 1, 256, "grid.height")
        if width * height > MAX_CELLS:
            raise ValueError("Collision grid is too large")
        for key in ("walkable", "elevation", "encounter_type"):
            rows = grid.get(key)
            if not isinstance(rows, list) or len(rows) != height or any(not isinstance(row, list) or len(row) != width for row in rows):
                raise ValueError("Malformed " + key + " grid")
        for row in grid["walkable"]:
            if any(type(value) is not bool for value in row):
                raise ValueError("Walkability values must be known booleans")
        warps = {_point(point) for point in grid.get("warp_positions", [])}
        if target in warps and goal.get("allow_target_warp") is not True:
            return self._stop("paused", "The target is a warp; explicit warp permission is required.")
        # Recompute every step from current NPC positions and observed obstruction tiles.
        path = plan_path(grid, position, target, blocked=self.blocked_tiles,
                         avoid_encounters=bool(goal.get("avoid_encounters", True)))
        if not path:
            return self._stop("paused", "No permitted walking path remains; request a visual decision.")
        button = path[0]
        dx, dy = _DIRECTIONS[button]
        expected = position[0] + dx, position[1] + dy
        if self._cancel.is_set() or _cancelled(cancel):
            return self._stop("cancelled", "Control was cancelled before issuing the next step.")
        self.pending = {"frame": frame, "start": position, "expected": expected, "button": button,"facing":world.get('facing')}
        note = f"Walking one tile {button.lower()} toward ({target[0]}, {target[1]}); I will check the new position."
        action = {"buttons": [button], "frames": step_frames,
                  "objective": f"Reach ({target[0]}, {target[1]}) on map {goal_map}",
                  "note": note, "events": [], "pause": False}
        return self._decision("step", note, action=action, path_length=len(path),
                              issued_at_frame=frame, expected_position=list(expected))


_default_lock = threading.RLock()
_default_session = NavigationSession()


def choose_navigation_action(world: dict, goal: dict, *, state: NavigationSession | None = None,
                             cancel: Any = None, epoch: int | None = None) -> dict:
    """Choose one step; pass a session for explicit lifecycle and stuck tracking.

    With no session supplied a single shared session is used. It deliberately
    stays stopped after arrival, cancellation, or changed goal. Production
    callers should construct NavigationSession for each new confirmed goal.
    """
    if state is not None:
        return state.choose(world, goal, cancel=cancel, epoch=epoch)
    with _default_lock:
        return _default_session.choose(world, goal, cancel=cancel, epoch=epoch)
