"""Pure navigation regression tests; no emulator, services, or input required."""
import copy
import threading
import unittest

from navigation import NavigationSession, choose_navigation_action


def snapshot(position=(1, 1), frame=100):
    return {"valid": True, "validated": True, "in_battle": False, "map_id": "4.3",
            "position": {"x": position[0], "y": position[1]}, "frame": frame,
            "position_sources_agree": True,
            "avatar_candidate": {"prevent_step": False, "running_state": 0, "tile_transition_state": 0},
            "grid": {"validated": True, "width": 5, "height": 5,
                     "walkable": [[True] * 5 for _ in range(5)],
                     "elevation": [[0] * 5 for _ in range(5)],
                     "encounter_type": [[0] * 5 for _ in range(5)],
                     "warp_positions": [], "blocked_positions": []}}


def goal(target=(3, 1)):
    return {"map_id": "4.3", "target": list(target), "step_frames": 16,
            "calibration_validated": True, "overworld_confirmed": True}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.session = NavigationSession()

    def choose(self, world=None, destination=None, **kwargs):
        return choose_navigation_action(world or snapshot(), destination or goal(),
                                        state=self.session, epoch=kwargs.pop("epoch", 7), **kwargs)

    def test_closed_loop_waits_and_arrives(self):
        first = self.choose()
        self.assertEqual(first["status"], "step")
        self.assertEqual(first["action"]["buttons"], ["RIGHT"])
        self.assertEqual(self.choose(snapshot(frame=100))["status"], "waiting")
        self.assertEqual(self.choose(snapshot(frame=117))["status"], "waiting")
        second = self.choose(snapshot((2, 1), 122))
        self.assertEqual(second["status"], "step")
        self.assertEqual(second["successful_steps"], 1)
        self.assertEqual(self.choose(snapshot((3, 1), 144))["status"], "arrived")
        self.assertIsNone(self.choose(snapshot((3, 1), 200))["action"])

    def test_cancel_and_epoch_fences(self):
        self.choose()
        event = threading.Event()
        event.set()
        self.assertEqual(self.choose(cancel=event)["status"], "cancelled")
        self.assertIsNone(self.choose()["action"])
        other = NavigationSession()
        other.choose(snapshot(), goal(), epoch=8)
        self.assertEqual(other.choose(snapshot(frame=125), goal(), epoch=9)["status"], "cancelled")

    def test_requires_validation_calibration_and_frame(self):
        for mutate in (lambda w: w.update(validated=False),
                       lambda w: w.pop("frame"),
                       lambda w: w["grid"].update(validated=False)):
            world = snapshot(); mutate(world)
            decision = NavigationSession().choose(world, goal(), epoch=7)
            self.assertEqual(decision["status"], "paused")
            self.assertIsNone(decision["action"])
        target = goal(); target["calibration_validated"] = False
        self.assertEqual(self.choose(destination=target)["status"], "paused")

    def test_no_progress_reroutes_and_stops(self):
        self.choose()
        failed = self.choose(snapshot(frame=122))
        self.assertEqual(failed["failed_steps"], 1)
        self.assertNotEqual(failed["action"]["buttons"], ["RIGHT"])
        self.choose(snapshot(frame=144))
        third = self.choose(snapshot(frame=166))
        self.assertEqual(third["status"], "paused")
        self.assertIn("no progress", third["reason"])

    def test_overshoot_battle_dialogue_and_map_change_stop(self):
        self.choose()
        self.assertEqual(self.choose(snapshot((4, 1), 130))["status"], "paused")
        for key, value in (("in_battle", True), ("dialogue_active", True), ("menu_active", True), ("map_id", "4.4")):
            world = snapshot(); world[key] = value
            self.assertEqual(NavigationSession().choose(world, goal(), epoch=7)["status"], "paused")

    def test_busy_player_waits_then_times_out(self):
        self.choose()
        world = snapshot(frame=123)
        world["avatar_candidate"]["running_state"] = 2
        self.assertEqual(self.choose(world)["status"], "waiting")
        world["frame"] = 241
        self.assertEqual(self.choose(world)["status"], "paused")

    def test_npcs_grass_and_warps_are_respected(self):
        world = snapshot()
        world["grid"]["blocked_positions"] = [{"x": 2, "y": 1}]
        self.assertNotEqual(self.choose(world)["action"]["buttons"], ["RIGHT"])
        world = snapshot()
        world["grid"]["encounter_type"][1][2] = 1
        decision = NavigationSession().choose(world, goal(), epoch=7)
        self.assertNotEqual(decision["action"]["buttons"], ["RIGHT"])
        world["grid"]["warp_positions"] = [{"x": 3, "y": 1}]
        self.assertEqual(NavigationSession().choose(world, goal(), epoch=7)["status"], "paused")

    def test_input_world_and_goal_are_not_mutated(self):
        world, target = snapshot(), goal()
        before_world, before_goal = copy.deepcopy(world), copy.deepcopy(target)
        self.choose(world, target)
        self.assertEqual(world, before_world)
        self.assertEqual(target, before_goal)


if __name__ == "__main__":
    unittest.main()
