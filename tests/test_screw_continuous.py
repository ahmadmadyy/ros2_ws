#!/usr/bin/env python3
"""
Continuous-screwing simulation — TOP-DOWN grasp.

Simulates a realistic hand-screwdriver operation where the wrist has a finite
joint range, requiring a lift-and-reposition between strokes.

Sequence:
  Pick phase (same as test_screw_topdown.py):
    1–10. Pick screwdriver top-down, attach, move above screw, lower to engage.

  Screw phase — N_CYCLES cycles of:
    (a) Rotate wrist_3 CW in 45° steps until WRIST3_SAFE_MAX (~+360°).
    (b) Lift LIFT_H cm straight up — IK uses the CURRENT (rotated) orientation
        so the solver keeps wrist_3 in place and only adjusts the arm height.
    (c) Rotate ONLY wrist_3 CCW to WRIST3_SAFE_MIN (~-360°) — pure joint move,
        no reorientation of the rest of the arm.
    (d) Lower back to engage height [skip on last cycle].

  Return:
    11. Open gripper — drop screwdriver.
    12. Detach screwdriver from tool0.
    13. Return home.

Key design — orientation tracking:
  After wrist_3 rotates by Δ from the initial top-down pose q=(1,0,0,0),
  the new end-effector orientation is:
      q_rot = (cos(Δ/2), -sin(Δ/2), 0, 0)
  IK called with this orientation finds joints where wrist_3 ≈ initial + Δ,
  i.e. the arm only adjusts height and leaves wrist_3 where it is.

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
SCREW_TOOL0_Z    = GRASP_Z            # 0.26 m — tip at z=0.0
SCREW_APPROACH_Z = SCREW_TOOL0_Z + 0.15

# ---------------------------------------------------------------------------
# Continuous-screwing parameters
# ---------------------------------------------------------------------------
N_CYCLES = 5

# wrist_3 joint limits ≈ ±2π rad; 0.3 rad safety margin from each hard stop
WRIST3_SAFE_MIN = -(2 * math.pi - 0.3)   # ≈ -5.983 rad
WRIST3_SAFE_MAX =  (2 * math.pi - 0.3)   # ≈ +5.983 rad

SCREW_STEP_RAD = +(math.pi / 4)   # +45° per CW step (j6 increasing = CW from above)

# Small lift height used to disengage the screwdriver tip between strokes
LIFT_H = 0.06   # 6 cm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pose(x, y, z, qx, qy, qz, qw):
    p = Pose()
    p.position.x, p.position.y, p.position.z = x, y, z
    p.orientation.x, p.orientation.y = qx, qy
    p.orientation.z, p.orientation.w = qz, qw
    return p


def q_from_wrist3_delta(initial_wrist3, current_wrist3):
    """Return (qx, qy, qz, qw) for the end-effector orientation after
    wrist_3 has moved by (current - initial) rad from the base top-down
    pose q_initial = (1, 0, 0, 0).

    Derivation (quaternion product q_initial ⊗ q_local_z(Δ)):
        q_initial = (qx=1, qy=0, qz=0, qw=0)  [180° about world x]
        q_local_z(Δ) = (0, 0, sin(Δ/2), cos(Δ/2))
        result = (cos(Δ/2), -sin(Δ/2), 0, 0)
    IK targeting this orientation finds wrist_3 ≈ initial + Δ,
    keeping the arm's shoulder/elbow joints mostly unchanged.
    """
    d = current_wrist3 - initial_wrist3
    return math.cos(d / 2), -math.sin(d / 2), 0.0, 0.0


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


def _ik_first(moveit, pose, seeds):
    """Return first successful IK result from the seed list, or None."""
    for seed in seeds:
        j = moveit.ik(pose, seed_joints=seed)
        if j is not None:
            return j
    return None


def detach_screwdriver(node, object_id: str, link_name: str) -> bool:
    """Detach a collision object from a robot link and return it to the world."""
    from moveit_msgs.srv import ApplyPlanningScene
    from moveit_msgs.msg import PlanningScene, AttachedCollisionObject, CollisionObject

    scene = PlanningScene()
    scene.is_diff = True

    aco = AttachedCollisionObject()
    aco.link_name = link_name
    aco.object.id = object_id
    aco.object.operation = CollisionObject.REMOVE
    scene.robot_state.attached_collision_objects.append(aco)
    scene.robot_state.is_diff = True

    from robot_agent.moveit_client import _wait_for_future
    client = node.create_client(ApplyPlanningScene, '/apply_planning_scene')
    if not client.wait_for_service(timeout_sec=5.0):
        node.get_logger().error('detach_screwdriver: /apply_planning_scene not available')
        return False

    req = ApplyPlanningScene.Request()
    req.scene = scene
    result = _wait_for_future(client.call_async(req), timeout_sec=5.0)
    ok = result is not None and result.success
    if ok:
        node.get_logger().info(f"Detached '{object_id}' from '{link_name}'")
    else:
        node.get_logger().error(f"Failed to detach '{object_id}' from '{link_name}'")
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

        # ------------------------------------------------------------------
        # Shared mutable state
        # ------------------------------------------------------------------
        _engage_joints = [None]

        def lower_to_engage_initial():
            for seed in [_last_joints[0], PICK_SEED, HOME_JOINTS]:
                if seed is None:
                    continue
                j = moveit.ik(screw_engage_pose, seed_joints=seed)
                if j is not None:
                    ok = moveit.plan_and_execute_joints(j, velocity_scaling=0.15)
                    if ok:
                        _engage_joints[0] = list(j)
                        _last_joints[0]   = list(j)
                    return ok
            node.get_logger().error("IK failed for initial engage pose")
            return False

        # ------------------------------------------------------------------
        # Continuous screwing cycles
        # ------------------------------------------------------------------
        def run_screw_cycles():
            """
            N_CYCLES of:
              (a) CW wrist_3 rotation to WRIST3_SAFE_MAX  (engaged with screw)
              (b) Small lift  — IK with current rotated orientation keeps wrist_3
              (c) CCW wrist_3 to WRIST3_SAFE_MIN  — ONLY joint 5 moves
              (d) Lower back to engage height  [skipped on last cycle]
            """
            # Capture the canonical wrist_3 from the initial IK solution.
            # All orientation calculations are relative to this value.
            initial_wrist3 = _engage_joints[0][5]
            total_cw_rad   = 0.0

            for cycle in range(1, N_CYCLES + 1):
                node.get_logger().info(
                    f"\n--- Cycle {cycle}/{N_CYCLES}  "
                    f"(start wrist_3={_engage_joints[0][5]:.3f} rad) ---"
                )

                # ---- (a) CW screwing until lower joint limit ----
                joints   = list(_engage_joints[0])
                start_w3 = joints[5]
                cw_steps = 0

                while joints[5] + SCREW_STEP_RAD <= WRIST3_SAFE_MAX:
                    joints[5] += SCREW_STEP_RAD
                    if not moveit.plan_and_execute_joints(joints, velocity_scaling=0.2):
                        node.get_logger().error(f"CW step {cw_steps + 1} failed")
                        return False
                    cw_steps += 1
                    time.sleep(0.1)

                stroke_rad   = joints[5] - start_w3
                total_cw_rad += stroke_rad
                node.get_logger().info(
                    f"  CW: {cw_steps} steps × 45°, "
                    f"{math.degrees(stroke_rad):.0f}° this stroke, "
                    f"{math.degrees(total_cw_rad):.0f}° cumulative"
                )

                # ---- (b) Small lift — orientation matches current wrist_3 ----
                # q_rot = q_from_wrist3_delta ensures IK keeps wrist_3 in place
                # and only adjusts shoulder/elbow to raise z by LIFT_H.
                qx, qy, qz, qw = q_from_wrist3_delta(initial_wrist3, joints[5])
                lift_pose   = make_pose(SCREW_X, SCREW_Y, SCREW_TOOL0_Z + LIFT_H,
                                        qx, qy, qz, qw)
                lift_joints = _ik_first(moveit, lift_pose, [joints, PICK_SEED])
                if lift_joints is None:
                    node.get_logger().error(f"Lift IK failed (cycle {cycle})")
                    return False
                if not moveit.plan_and_execute_joints(lift_joints, velocity_scaling=0.3):
                    node.get_logger().error(f"Lift motion failed (cycle {cycle})")
                    return False

                # ---- (c) CCW rotation — ONLY wrist_3 (joint 5) changes ----
                ccw_joints    = list(lift_joints)
                ccw_joints[5] = WRIST3_SAFE_MIN
                if not moveit.plan_and_execute_joints(ccw_joints, velocity_scaling=0.5):
                    node.get_logger().error(f"CCW reset failed (cycle {cycle})")
                    return False
                node.get_logger().info(
                    f"  CCW reset → wrist_3={WRIST3_SAFE_MIN:.3f} rad")

                # ---- (d) Lower to engage / full lift on last cycle ----
                if cycle < N_CYCLES:
                    # Orientation at WRIST3_SAFE_MIN — IK keeps wrist_3 near min
                    qx, qy, qz, qw = q_from_wrist3_delta(initial_wrist3, WRIST3_SAFE_MIN)
                    lower_pose  = make_pose(SCREW_X, SCREW_Y, SCREW_TOOL0_Z,
                                            qx, qy, qz, qw)
                    re_engage   = _ik_first(moveit, lower_pose, [ccw_joints, PICK_SEED])
                    if re_engage is None:
                        node.get_logger().error(f"Lower IK failed (cycle {cycle})")
                        return False
                    if not moveit.plan_and_execute_joints(re_engage, velocity_scaling=0.15):
                        node.get_logger().error(f"Lower motion failed (cycle {cycle})")
                        return False
                    _engage_joints[0] = list(re_engage)
                    node.get_logger().info(
                        f"  Lowered: wrist_3={re_engage[5]:.3f} rad")
                else:
                    # Last cycle: lift fully to approach height for safe home move
                    qx, qy, qz, qw = q_from_wrist3_delta(initial_wrist3, WRIST3_SAFE_MIN)
                    exit_pose   = make_pose(SCREW_X, SCREW_Y, SCREW_APPROACH_Z,
                                            qx, qy, qz, qw)
                    exit_joints = _ik_first(moveit, exit_pose, [ccw_joints, PICK_SEED])
                    if exit_joints is not None:
                        moveit.plan_and_execute_joints(exit_joints, velocity_scaling=0.3)
                        _last_joints[0] = list(exit_joints)
                    else:
                        _last_joints[0] = list(ccw_joints)

                time.sleep(0.2)

            node.get_logger().info(
                f"\n=== {N_CYCLES} cycles complete — "
                f"{math.degrees(total_cw_rad):.0f}° total CW "
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
             lambda: ik_and_move(moveit, retreat_pose, 0.3,
                                 [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Move above screw target",
             lambda: ik_and_move(moveit, screw_approach_pose, 0.3,
                                 [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Lower to engage screw (initial)",
             lower_to_engage_initial),
            (f"Continuous screw — {N_CYCLES} cycles "
             f"(CW to limit → lift → CCW only wrist_3 → lower)",
             run_screw_cycles),
            ("Open gripper — drop screwdriver",
             lambda: gripper.open()),
            ("Detach screwdriver from tool0",
             lambda: detach_screwdriver(node, "screwdriver", "tool0")),
            ("Return home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 66)
        print("  Continuous Screwing — TOP-DOWN grasp")
        print(f"  Pick   x={SCREWDRIVER_X:.2f}  y={SCREWDRIVER_Y:.2f}  "
              f"tool0-z={GRASP_Z:.3f} m")
        print(f"  Screw  x={SCREW_X:.2f}  y={SCREW_Y:.2f}  "
              f"tool0-z={SCREW_TOOL0_Z:.3f} m")
        print(f"  Cycles N={N_CYCLES},  step={math.degrees(abs(SCREW_STEP_RAD)):.0f}°/step CW")
        print(f"  wrist_3 range: [{WRIST3_SAFE_MIN:.2f}, {WRIST3_SAFE_MAX:.2f}] rad  "
              f"lift={LIFT_H*100:.0f} cm between strokes")
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
        print(f"  Result: {'SUCCESS' if all_ok else 'FAILED — see above.'}")
        print("=" * 66 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
