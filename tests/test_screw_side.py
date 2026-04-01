#!/usr/bin/env python3
"""
Pick-and-screw with SIDE grasp.

Sequence:
  1. Home
  2. Open gripper
  3. Approach screwdriver from +y side (horizontal, z = screwdriver midpoint)
  4. Allow gripper ↔ screwdriver collision
  5. Slide in horizontally to grasp pose
  6. Close gripper onto screwdriver shaft
  7. Attach screwdriver to gripper in planning scene
  8. Retreat horizontally back in +y direction
  9. Reorient arm to top-down above screw target
 10. Lower to engage screw (screwdriver tip at table surface)
 11. Rotate wrist_3 CW × SCREW_TURNS turns (screwing motion)
 12. Lift after screwing
 13. Return home

Geometry notes — side grasp orientation:
  - Screwdriver: 24 mm diameter, 180 mm tall, upright at x=0.45, y=0.00.
  - Gripper approaches horizontally from +y (tool0 z-axis in world -y direction).
  - Quaternion (qx=√2/2, qy=0, qz=0, qw=√2/2) = 90° rotation about world x:
      tool0 z → world -y  (approach direction)
      tool0 x → world +x  (Robotiq finger closing axis → grips shaft in ±x)
      tool0 y → world +z
  - Finger tips 0.170 m from tool0 along tool0 z-axis (= world -y):
      finger tips at y = tool0_y - 0.170
      → grasp pose: tool0 at (0.45, 0.170, 0.09), tips at (0.45, 0.00, 0.09)
  - Grasp height z=0.09: screwdriver midpoint (180 mm / 2 = 90 mm above table).
  - Approach offset in +y: 0.18 m beyond grasp pose
      → approach pose: tool0 at (0.45, 0.350, 0.09)

Screwing geometry (same as top-down, after arm reorients):
  - Screw flush with table (z=0.0). Tool0 at z=0.26 puts tip at z=0.0.

Prerequisites (Terminal 1):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
    python3 ~/ros2_ws/tests/test_screw_side.py
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
SCREWDRIVER_MID_Z  = 0.09    # half of 180 mm height = grasp height

# ---------------------------------------------------------------------------
# Side-grasp orientation
# ---------------------------------------------------------------------------
# Approach from +y (tool0 z-axis in world -y direction).
# 90° rotation about world x-axis:
#   tool0 z → world -y  (approach)
#   tool0 x → world +x  (Robotiq closing axis → grips shaft in ±x)
#   tool0 y → world +z
# Quaternion: q = (sin(45°), 0, 0, cos(45°)) = (√2/2, 0, 0, √2/2)
_S45 = math.sqrt(2) / 2
SIDE_QX, SIDE_QY, SIDE_QZ, SIDE_QW = _S45, 0.0, 0.0, _S45

# Y-coordinate of tool0 at grasp (finger tips reach y=0.00):
SIDE_GRASP_Y    = SCREWDRIVER_Y + FINGER_TIP_OFFSET   # 0.170 m
SIDE_APPROACH_Y = SIDE_GRASP_Y + 0.18                  # 0.350 m (18 cm back)
SIDE_GRASP_Z    = SCREWDRIVER_MID_Z                     # 0.090 m

# Gripper: ~24 mm gap for screwdriver shaft
GRASP_GRIPPER_POSITION = 0.57

HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]

# IK seed for side approach: arm reaching +x with wrist tilted horizontal.
# wrist_2_joint ≈ -π/2 tilts the tool to point sideways (-y).
# wrist_3_joint ≈ π/2 rolls the tool so tool0 x aligns with world +x
# (Robotiq closing axis in ±x direction to grip the shaft).
SIDE_SEED = [0.0, -1.3, 1.6, -1.5, -math.pi / 2, math.pi / 2]

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
# Screw target geometry (top-down screwing after reorientation)
# ---------------------------------------------------------------------------
# After picking from the side, the arm reorients to top-down for screwing.
# Screw is at z=0.0; tool0 must be at z=0.26 so the tip reaches z=0.0.
SCREW_X          = 0.40
SCREW_Y          = 0.10
SCREW_TOOL0_Z    = SCREWDRIVER_MID_Z + FINGER_TIP_OFFSET   # 0.26 m
SCREW_APPROACH_Z = SCREW_TOOL0_Z + 0.15                     # 0.41 m

# Top-down orientation for screwing: 180° about X → tool0 z in world -z
SCREW_QX, SCREW_QY, SCREW_QZ, SCREW_QW = 1.0, 0.0, 0.0, 0.0

# Screwing motion: 3 full CW turns, 4 steps per turn (90° increments)
# CW from above → increase wrist_3_joint (index 5)
SCREW_TURNS    = 3
STEPS_PER_TURN = 4
SCREW_STEP_RAD = +(2 * math.pi / STEPS_PER_TURN)   # +π/2 per step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pose(x, y, z, qx, qy, qz, qw):
    p = Pose()
    p.position.x, p.position.y, p.position.z = x, y, z
    p.orientation.x, p.orientation.y = qx, qy
    p.orientation.z, p.orientation.w = qz, qw
    return p


_last_joints = [None]   # tracks last successful IK result across steps


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

class ScrewSideNode(Node):
    def __init__(self):
        super().__init__("screw_side_test")


def main():
    rclpy.init()
    node = ScrewSideNode()

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
        # Side-grasp waypoints (horizontal approach from +y)
        # ------------------------------------------------------------------
        side_approach_pose = make_pose(
            SCREWDRIVER_X, SIDE_APPROACH_Y, SIDE_GRASP_Z,
            SIDE_QX, SIDE_QY, SIDE_QZ, SIDE_QW,
        )
        side_grasp_pose = make_pose(
            SCREWDRIVER_X, SIDE_GRASP_Y, SIDE_GRASP_Z,
            SIDE_QX, SIDE_QY, SIDE_QZ, SIDE_QW,
        )
        side_retreat_pose = make_pose(
            SCREWDRIVER_X, SIDE_APPROACH_Y, SIDE_GRASP_Z,
            SIDE_QX, SIDE_QY, SIDE_QZ, SIDE_QW,
        )

        # ------------------------------------------------------------------
        # Screw waypoints (top-down orientation after reorientation)
        # ------------------------------------------------------------------
        pre_screw_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_APPROACH_Z,
            SCREW_QX, SCREW_QY, SCREW_QZ, SCREW_QW,
        )
        screw_engage_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_TOOL0_Z,
            SCREW_QX, SCREW_QY, SCREW_QZ, SCREW_QW,
        )
        screw_retreat_pose = make_pose(
            SCREW_X, SCREW_Y, SCREW_APPROACH_Z,
            SCREW_QX, SCREW_QY, SCREW_QZ, SCREW_QW,
        )

        # ------------------------------------------------------------------
        # Screwing steps
        # ------------------------------------------------------------------
        _engage_joints = [None]

        def lower_to_engage():
            for seed in [_last_joints[0], SCREW_SEED, HOME_JOINTS]:
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
                joints[5] += SCREW_STEP_RAD   # wrist_3_joint (index 5)
                ok = moveit.plan_and_execute_joints(joints, velocity_scaling=0.15)
                if not ok:
                    node.get_logger().error(f"Screw rotation step {step + 1} failed")
                    return False
                time.sleep(0.2)
            return True

        # ------------------------------------------------------------------
        # Step table
        # ------------------------------------------------------------------
        SCREW_SEED = [0.0, -1.7, 1.8, -1.7, -math.pi / 2, 0.0]
        _last_joints[0] = list(SIDE_SEED)

        steps = [
            ("Home position",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
            ("Open gripper",
             lambda: gripper.open()),
            # Side approach: try SIDE_SEED first to force horizontal IK branch
            ("Approach screwdriver (side, +y direction)",
             lambda: ik_and_move(moveit, side_approach_pose, 0.3, [SIDE_SEED, HOME_JOINTS])),
            ("Allow gripper ↔ screwdriver collision",
             lambda: moveit.allow_collision("screwdriver")),
            # Slide in: use last joints (approach result) as first seed
            ("Slide in to grasp (horizontal)",
             lambda: ik_and_move(moveit, side_grasp_pose, 0.2, [_last_joints[0], SIDE_SEED])),
            ("Close gripper onto shaft",
             lambda: gripper.set_position(GRASP_GRIPPER_POSITION)),
            ("Attach screwdriver to tool0",
             lambda: moveit.attach_object("screwdriver", "tool0", touch_links=GRIPPER_LINKS)),
            # Retreat: same orientation, last joints first
            ("Retreat in +y direction",
             lambda: ik_and_move(moveit, side_retreat_pose, 0.3, [_last_joints[0], SIDE_SEED])),
            # Reorient: transition to top-down — use SCREW_SEED for clean IK branch
            ("Reorient to top-down above screw target",
             lambda: ik_and_move(moveit, pre_screw_pose, 0.3, [SCREW_SEED, HOME_JOINTS])),
            ("Lower to engage screw",
             lower_to_engage),
            (f"Screw CW × {SCREW_TURNS} turns ({SCREW_TURNS * STEPS_PER_TURN} steps)",
             do_screw_motion),
            ("Lift off screw",
             lambda: ik_and_move(moveit, screw_retreat_pose, 0.3, [_last_joints[0], SCREW_SEED, HOME_JOINTS])),
            ("Return home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 62)
        print("  Screwdriver Pick + Screw — SIDE grasp (approach from +y)")
        print(f"  Pick   x={SCREWDRIVER_X:.2f}  y={SCREWDRIVER_Y:.2f}  "
              f"grasp-z={SIDE_GRASP_Z:.3f} m")
        print(f"  Grasp orientation: qx={SIDE_QX} qy={SIDE_QY} "
              f"qz={SIDE_QZ} qw={SIDE_QW}")
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
