#!/usr/bin/env python3
"""
Pick-and-screw with TOP-DOWN grasp.

Sequence:
  1. Home
  2. Open gripper
  3. Approach screwdriver from above (z = GRASP_Z + APPROACH_H)
  4. Allow gripper ↔ screwdriver collision
  5. Descend to grasp (z = GRASP_Z)
  6. Close gripper onto screwdriver shaft
  7. Attach screwdriver to gripper in planning scene
  8. Retreat upward (z = GRASP_Z + RETREAT_H)
  9. Move above screw target (SCREW_X, SCREW_Y, SCREW_APPROACH_Z)
 10. Lower to engage screw (z = SCREW_TOOL0_Z, tip touches table surface)
 11. Rotate wrist_3 CW × SCREW_TURNS turns (screwing motion)
 12. Lift after screwing
 13. Return home

Geometry notes:
  - Screwdriver: 24 mm diameter cylinder, 180 mm tall, standing upright at
    x=0.45, y=0.00. Top-down grasp at midpoint → finger tips at z=0.09,
    tool0 at z=0.26 (GRASP_Z).
  - Finger tip offset: 0.170 m below tool0 along tool0 z-axis.
  - Screwdriver tip is 0.09 m below finger tips → at z=0.0 (table surface).
  - SCREW_TOOL0_Z = 0.26 positions the tip at the table screw (z=0.0).

Prerequisites (Terminal 1):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    python3 ~/ros2_ws/tests/test_screw_topdown.py
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
# Screwdriver geometry
# ---------------------------------------------------------------------------
SCREWDRIVER_X      = 0.45
SCREWDRIVER_Y      = 0.00
SCREWDRIVER_RADIUS = 0.012   # 12 mm radius (24 mm diameter)

FINGER_TIP_OFFSET  = 0.170   # tool0 → finger tips along tool0 z-axis
SCREWDRIVER_MID_Z  = 0.09    # half of 180 mm height

# tool0 height at grasp (finger tips at screwdriver midpoint z=0.09)
GRASP_Z    = SCREWDRIVER_MID_Z + FINGER_TIP_OFFSET   # 0.26 m
APPROACH_H = 0.18   # m above GRASP_Z for pre-grasp waypoint
RETREAT_H  = 0.20   # m above GRASP_Z for post-grasp lift

# Top-down orientation: 180° rotation about X → tool0 z points in world -z
GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW = 1.0, 0.0, 0.0, 0.0

# Gripper: ~24 mm gap  → 0.57 rad  (0.79 × (85-24)/85)
GRASP_GRIPPER_POSITION = 0.57

HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]

# Seed close to the expected arm configuration when tool0 is above/at the
# screwdriver (x=0.45, y=0.00, z≈0.26–0.44) with top-down orientation.
# Keeps IK solutions near this region so the planner only moves the arm slightly
# between approach → descend → retreat → pre_screw → engage.
PICK_SEED = [0.0, -1.7, 1.8, -1.7, -math.pi / 2, 0.0]

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
# Screw is flush with the table (z=0.0).
# When the screwdriver tip (0.26 m below tool0) touches the screw,
# tool0 must be at z = screw_z + 0.26.
SCREW_X       = 0.40
SCREW_Y       = 0.10
SCREW_TOOL0_Z = GRASP_Z          # 0.26 m — tip at z=0.0 (table surface)
SCREW_APPROACH_Z = SCREW_TOOL0_Z + 0.15   # 15 cm safety clearance above

# Screwing motion: 3 full CW turns, 4 steps per turn (90° increments)
# Clockwise when viewed from above → decrease wrist_3_joint value
SCREW_TURNS     = 3
STEPS_PER_TURN  = 4
SCREW_STEP_RAD  = -(2 * math.pi / STEPS_PER_TURN)   # -π/2 per step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pose(x, y, z, qx, qy, qz, qw):
    p = Pose()
    p.position.x, p.position.y, p.position.z = x, y, z
    p.orientation.x, p.orientation.y = qx, qy
    p.orientation.z, p.orientation.w = qz, qw
    return p


_last_joints = [None]   # tracks the last successful IK result across steps


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

class ScrewTopdownNode(Node):
    def __init__(self):
        super().__init__("screw_topdown_test")


def main():
    rclpy.init()
    node = ScrewTopdownNode()

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
        pre_screw_pose = make_pose(
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
        # Screwing steps — capture IK joints at engage pose, then rotate
        # ------------------------------------------------------------------
        _engage_joints = [None]   # mutable holder for closure

        def lower_to_engage():
            # Use last arm config (robot is at pre_screw_pose) as primary seed
            for seed in [_last_joints[0], PICK_SEED, HOME_JOINTS]:
                if seed is None:
                    continue
                joints = moveit.ik(screw_engage_pose, seed_joints=seed)
                if joints is not None:
                    ok = moveit.plan_and_execute_joints(joints, velocity_scaling=0.15)
                    if ok:
                        _engage_joints[0] = list(joints)
                        _last_joints[0] = list(joints)
                    return ok
            node.get_logger().error("IK failed for screw engage pose")
            return False

        def do_screw_motion():
            joints = _engage_joints[0]
            if joints is None:
                return False
            for step in range(SCREW_TURNS * STEPS_PER_TURN):
                joints[5] += SCREW_STEP_RAD   # rotate wrist_3 (index 5)
                ok = moveit.plan_and_execute_joints(joints, velocity_scaling=0.15)
                if not ok:
                    node.get_logger().error(f"Screw rotation step {step + 1} failed")
                    return False
                time.sleep(0.2)
            return True

        # ------------------------------------------------------------------
        # Step table
        # ------------------------------------------------------------------
        # Reset seed tracker at the start of each run
        _last_joints[0] = list(PICK_SEED)

        steps = [
            ("Home position",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
            ("Open gripper",
             lambda: gripper.open()),
            # Approach: PICK_SEED first forces the arm-forward IK branch
            ("Approach screwdriver (top-down)",
             lambda: ik_and_move(moveit, approach_pose, 0.3, [PICK_SEED, HOME_JOINTS])),
            ("Allow gripper ↔ screwdriver collision",
             lambda: moveit.allow_collision("screwdriver")),
            # Descent: use last joints (approach result) then PICK_SEED — same branch
            ("Descend to grasp",
             lambda: ik_and_move(moveit, grasp_pose, 0.2, [_last_joints[0], PICK_SEED])),
            ("Close gripper onto shaft",
             lambda: gripper.set_position(GRASP_GRIPPER_POSITION)),
            ("Attach screwdriver to tool0",
             lambda: moveit.attach_object("screwdriver", "tool0", touch_links=GRIPPER_LINKS)),
            ("Retreat upward",
             lambda: ik_and_move(moveit, retreat_pose, 0.3, [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Move above screw target",
             lambda: ik_and_move(moveit, pre_screw_pose, 0.3, [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Lower to engage screw",
             lower_to_engage),
            (f"Screw CW × {SCREW_TURNS} turns ({SCREW_TURNS * STEPS_PER_TURN} steps)",
             do_screw_motion),
            ("Lift off screw",
             lambda: ik_and_move(moveit, screw_retreat_pose, 0.3, [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Return home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 62)
        print("  Screwdriver Pick + Screw — TOP-DOWN grasp")
        print(f"  Pick   x={SCREWDRIVER_X:.2f}  y={SCREWDRIVER_Y:.2f}  "
              f"tool0-z={GRASP_Z:.3f} m")
        print(f"  Screw  x={SCREW_X:.2f}  y={SCREW_Y:.2f}  "
              f"tool0-z={SCREW_TOOL0_Z:.3f} m")
        print(f"  Motion {SCREW_TURNS} CW turns × {STEPS_PER_TURN} steps "
              f"({math.degrees(abs(SCREW_STEP_RAD)):.0f}° each)")
        print("=" * 62)

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

        print("\n" + "=" * 62)
        print(f"  Result: {'SUCCESS — screw complete!' if all_ok else 'FAILED — see above.'}")
        print("=" * 62 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
