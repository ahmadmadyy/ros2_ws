#!/usr/bin/env python3
"""
Continuous-screwing simulation — TOP-DOWN grasp.

Simulates a realistic hand-screwdriver operation where the wrist has a finite
joint range, so it must lift and reposition between strokes.

Sequence:
  Pick phase (same as test_screw_topdown.py):
    1. Home
    2. Open gripper
    3. Approach screwdriver from above (top-down)
    4. Allow gripper ↔ screwdriver collision
    5. Descend to grasp
    6. Close gripper onto screwdriver shaft
    7. Attach screwdriver to gripper in planning scene
    8. Retreat upward
    9. Move above screw target
   10. Lower to engage screw (initial)

  Screw phase — N_CYCLES cycles of:
   (a) Rotate wrist_3 CW in SCREW_STEP_RAD increments until ≈ WRIST3_SAFE_MIN
   (b) Pull up to approach height (IK-based, retains current arm configuration)
   (c) Rotate wrist_3 CCW to WRIST3_SAFE_MAX (joint-space reposition)
   (d) [cycles 1..N-1] Lower to re-engage screw for next cycle

  Return phase:
   11. Continuous screwing ({N_CYCLES} cycles)
   12. Lift off / normalise after screwing
   13. Return home

Geometry notes:
  - Screwdriver: 24 mm diameter, 180 mm tall, upright at x=0.45, y=0.00.
  - GRASP_Z = 0.26 m (finger tips at screwdriver midpoint z=0.09).
  - SCREW_TOOL0_Z = 0.26 m → screwdriver tip at z=0.0 (table surface).
  - wrist_3 joint limits: ±2π ≈ ±6.28 rad.
    WRIST3_SAFE_MIN ≈ -5.98 rad, WRIST3_SAFE_MAX ≈ +5.98 rad (0.3 rad margin).
  - Each CW stroke from initial (~-1.7 rad) → ≈ 225° screwed.
  - Each subsequent CW stroke (from ~+4.6 rad) → ≈ 585° screwed.
  - Total N_CYCLES=5 strokes ≈ 2500–3200° of effective screwing.

Prerequisites (Terminal 1):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    python3 ~/ros2_ws/tests/test_screw_continuous.py
"""

import math
import sys
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState
from geometry_msgs.msg import Pose

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "robot_agent"))

from robot_agent.moveit_client import MoveItClient
from robot_agent.gripper_client import GripperClient

# ---------------------------------------------------------------------------
# Screwdriver / grasp geometry  (same as test_screw_topdown.py)
# ---------------------------------------------------------------------------
SCREWDRIVER_X      = 0.45
SCREWDRIVER_Y      = 0.00
SCREWDRIVER_RADIUS = 0.012

FINGER_TIP_OFFSET  = 0.170
SCREWDRIVER_MID_Z  = 0.09

GRASP_Z    = SCREWDRIVER_MID_Z + FINGER_TIP_OFFSET   # 0.26 m
APPROACH_H = 0.18
RETREAT_H  = 0.20

# Top-down orientation: 180° about world x → tool0 z points in world -z
GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW = 1.0, 0.0, 0.0, 0.0

GRASP_GRIPPER_POSITION = 0.57   # ~24 mm gap

HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]
PICK_SEED   = [0.0, -1.7, 1.8, -1.7, -math.pi / 2, 0.0]

GRIPPER_LINKS = [
    'tool0',
    'rq_robotiq_85_base_link',
    'rq_robotiq_85_left_knuckle_link',
    'rq_robotiq_85_right_knuckle_link',
    'rq_robotiq_85_left_finger_link',
    'rq_robotiq_85_right_finger_link',
    'rq_robotiq_85_left_inner_knuckle_link',
    'rq_robotiq_85_right_inner_knuckle_link',
    'rq_robotiq_85_left_finger_tip_link',
    'rq_robotiq_85_right_finger_tip_link',
]

# ---------------------------------------------------------------------------
# Screw target geometry
# ---------------------------------------------------------------------------
SCREW_X          = 0.40
SCREW_Y          = 0.10
SCREW_TOOL0_Z    = GRASP_Z           # 0.26 m — tip at z=0.0
SCREW_APPROACH_Z = SCREW_TOOL0_Z + 0.15

# ---------------------------------------------------------------------------
# Continuous-screwing parameters
# ---------------------------------------------------------------------------
N_CYCLES = 5

# wrist_3 joint limits ≈ ±2π; leave 0.3 rad margin from each hard stop
WRIST3_SAFE_MIN = -(2 * math.pi - 0.3)   # ≈ -5.983 rad
WRIST3_SAFE_MAX =  (2 * math.pi - 0.3)   # ≈ +5.983 rad

# Step size for CW screwing  (-45° increments)
SCREW_STEP_RAD = -(math.pi / 4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pose(x, y, z, qx, qy, qz, qw):
    p = Pose()
    p.position.x, p.position.y, p.position.z = x, y, z
    p.orientation.x, p.orientation.y = qx, qy
    p.orientation.z, p.orientation.w = qz, qw
    return p


_last_joints = [None]


def ik_and_move(moveit, pose, velocity_scaling=0.3, seeds=None):
    """Try each IK seed in order; store successful joints in _last_joints."""
    for seed in (seeds or [None]):
        joints = moveit.ik(pose, seed_joints=seed)
        if joints is not None:
            ok = moveit.plan_and_execute_joints(joints, velocity_scaling=velocity_scaling)
            if ok:
                _last_joints[0] = list(joints)
            return ok
    return False


def wait_for_joint_states(node, timeout_sec=20.0):
    received = threading.Event()
    sub = node.create_subscription(JointState, "/joint_states", lambda _: received.set(), 10)
    ok = received.wait(timeout_sec)
    node.destroy_subscription(sub)
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class ScrewContinuousNode(Node):
    def __init__(self):
        super().__init__("screw_continuous_test")


def main():
    rclpy.init()
    node = ScrewContinuousNode()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        node.get_logger().info("Waiting for /joint_states ...")
        if not wait_for_joint_states(node):
            node.get_logger().error("No /joint_states — is bringup running?")
            return

        moveit = MoveItClient(node)
        gripper = GripperClient(node)
        node.get_logger().info("MoveIt + gripper clients ready.")

        # ------------------------------------------------------------------
        # Cartesian waypoints
        # ------------------------------------------------------------------
        approach_pose = make_pose(
            SCREWDRIVER_X, SCREWDRIVER_Y, GRASP_Z + APPROACH_H,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )
        grasp_pose = make_pose(
            SCREWDRIVER_X, SCREWDRIVER_Y, GRASP_Z,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )
        retreat_pose = make_pose(
            SCREWDRIVER_X, SCREWDRIVER_Y, GRASP_Z + RETREAT_H,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )
        screw_approach_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_APPROACH_Z,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )
        screw_engage_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_TOOL0_Z,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )
        screw_retreat_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_APPROACH_Z,
            GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW,
        )

        # ------------------------------------------------------------------
        # Mutable state shared across closures
        # ------------------------------------------------------------------
        _engage_joints = [None]   # joints at the current screw-engage pose

        # ------------------------------------------------------------------
        # Initial lower-to-engage (first cycle setup)
        # ------------------------------------------------------------------
        def lower_to_engage_initial():
            for seed in [_last_joints[0], PICK_SEED, HOME_JOINTS]:
                if seed is None:
                    continue
                joints = moveit.ik(screw_engage_pose, seed_joints=seed)
                if joints is not None:
                    ok = moveit.plan_and_execute_joints(joints, velocity_scaling=0.15)
                    if ok:
                        _engage_joints[0] = list(joints)
                        _last_joints[0]   = list(joints)
                    return ok
            node.get_logger().error("IK failed for initial engage pose")
            return False

        # ------------------------------------------------------------------
        # Continuous screwing cycles
        # ------------------------------------------------------------------
        def run_screw_cycles():
            """
            N_CYCLES of:
              (a) CW wrist_3 rotation to WRIST3_SAFE_MIN
              (b) Lift to approach height
              (c) CCW wrist_3 rotation to WRIST3_SAFE_MAX
              (d) [not last cycle] Lower to re-engage
            """
            total_cw_rad = 0.0

            for cycle in range(1, N_CYCLES + 1):
                node.get_logger().info(
                    f"\n--- Screw cycle {cycle}/{N_CYCLES} "
                    f"(start wrist_3={_engage_joints[0][5]:.3f} rad) ---"
                )

                # ---- (a) CW screwing to lower joint limit ----
                joints    = list(_engage_joints[0])
                start_w3  = joints[5]
                cw_steps  = 0

                while joints[5] + SCREW_STEP_RAD >= WRIST3_SAFE_MIN:
                    joints[5] += SCREW_STEP_RAD          # CW = decreasing
                    ok = moveit.plan_and_execute_joints(joints, velocity_scaling=0.2)
                    if not ok:
                        node.get_logger().error(
                            f"CW step {cw_steps + 1} failed (cycle {cycle})")
                        return False
                    cw_steps += 1
                    time.sleep(0.1)

                stroke_rad  = start_w3 - joints[5]
                total_cw_rad += stroke_rad
                node.get_logger().info(
                    f"  CW done: {cw_steps} steps × 45°, "
                    f"{math.degrees(stroke_rad):.0f}° this stroke, "
                    f"{math.degrees(total_cw_rad):.0f}° cumulative, "
                    f"wrist_3={joints[5]:.3f} rad"
                )

                # ---- (b) Pull up to approach height ----
                lift_joints = None
                for seed in [joints, PICK_SEED]:
                    lift_joints = moveit.ik(screw_approach_pose, seed_joints=seed)
                    if lift_joints is not None:
                        break
                if lift_joints is None:
                    node.get_logger().error(f"Lift IK failed (cycle {cycle})")
                    return False
                ok = moveit.plan_and_execute_joints(lift_joints, velocity_scaling=0.3)
                if not ok:
                    node.get_logger().error(f"Lift motion failed (cycle {cycle})")
                    return False

                # ---- (c) CCW rotation to upper joint limit (reposition) ----
                ccw_joints = list(lift_joints)
                ccw_joints[5] = WRIST3_SAFE_MAX
                ok = moveit.plan_and_execute_joints(ccw_joints, velocity_scaling=0.5)
                if not ok:
                    node.get_logger().error(f"CCW reposition failed (cycle {cycle})")
                    return False
                node.get_logger().info(
                    f"  CCW reposition complete → wrist_3={WRIST3_SAFE_MAX:.3f} rad")

                # After last cycle leave arm at approach height; update _last_joints
                _last_joints[0] = list(ccw_joints)

                # ---- (d) Lower to re-engage (skip on last cycle) ----
                if cycle < N_CYCLES:
                    re_engage = None
                    for seed in [ccw_joints, PICK_SEED]:
                        re_engage = moveit.ik(screw_engage_pose, seed_joints=seed)
                        if re_engage is not None:
                            break
                    if re_engage is None:
                        node.get_logger().error(
                            f"Re-engage IK failed (cycle {cycle})")
                        return False
                    ok = moveit.plan_and_execute_joints(re_engage, velocity_scaling=0.15)
                    if not ok:
                        node.get_logger().error(
                            f"Re-engage motion failed (cycle {cycle})")
                        return False
                    _engage_joints[0] = list(re_engage)
                    node.get_logger().info(
                        f"  Re-engaged: wrist_3={re_engage[5]:.3f} rad")

                time.sleep(0.3)

            node.get_logger().info(
                f"\n=== All {N_CYCLES} cycles complete — "
                f"{math.degrees(total_cw_rad):.0f}° total CW screwing "
                f"({total_cw_rad / (2 * math.pi):.1f} full turns) ==="
            )
            return True

        # ------------------------------------------------------------------
        # Step table
        # ------------------------------------------------------------------
        _last_joints[0] = list(PICK_SEED)

        steps = [
            ("Home position",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
            ("Open gripper",
             lambda: gripper.open()),
            ("Approach screwdriver (top-down)",
             lambda: ik_and_move(moveit, approach_pose, 0.3, [PICK_SEED, HOME_JOINTS])),
            ("Allow gripper ↔ screwdriver collision",
             lambda: moveit.allow_collision("screwdriver")),
            ("Descend to grasp",
             lambda: ik_and_move(moveit, grasp_pose, 0.2, [_last_joints[0], PICK_SEED])),
            ("Close gripper onto shaft",
             lambda: gripper.set_position(GRASP_GRIPPER_POSITION)),
            ("Attach screwdriver to tool0",
             lambda: moveit.attach_object("screwdriver", "tool0", touch_links=GRIPPER_LINKS)),
            ("Retreat upward",
             lambda: ik_and_move(moveit, retreat_pose, 0.3, [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Move above screw target",
             lambda: ik_and_move(moveit, screw_approach_pose, 0.3, [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Lower to engage screw (initial)",
             lower_to_engage_initial),
            (f"Continuous screwing ({N_CYCLES} cycles: CW→lift→CCW→lower)",
             run_screw_cycles),
            ("Lift off / normalise orientation",
             lambda: ik_and_move(moveit, screw_retreat_pose, 0.3,
                                 [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Return home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 66)
        print("  Continuous Screwing — TOP-DOWN grasp")
        print(f"  Pick   x={SCREWDRIVER_X:.2f}  y={SCREWDRIVER_Y:.2f}  tool0-z={GRASP_Z:.3f} m")
        print(f"  Screw  x={SCREW_X:.2f}  y={SCREW_Y:.2f}  tool0-z={SCREW_TOOL0_Z:.3f} m")
        print(f"  Cycles N={N_CYCLES}  step={math.degrees(abs(SCREW_STEP_RAD)):.0f}°/step CW")
        print(f"  wrist_3 range: [{WRIST3_SAFE_MIN:.2f}, {WRIST3_SAFE_MAX:.2f}] rad")
        print("=" * 66)

        all_ok = True
        for i, (label, fn) in enumerate(steps, 1):
            print(f"\n[{i:2d}/{len(steps)}] {label}")
            t0 = time.monotonic()
            ok = fn()
            elapsed = time.monotonic() - t0
            print(f"         → {'OK' if ok else 'FAILED'}  ({elapsed:.2f} s)")
            if not ok:
                all_ok = False
                print("         Aborting sequence.")
                break
            time.sleep(0.4)

        print("\n" + "=" * 66)
        print(f"  Result: {'SUCCESS — continuous screw complete!' if all_ok else 'FAILED — see above.'}")
        print("=" * 66 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
