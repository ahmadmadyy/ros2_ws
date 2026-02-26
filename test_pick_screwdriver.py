#!/usr/bin/env python3
"""
Standalone pick test for UR5e + Robotiq 2F-85.

The robot picks a screwdriver that was added to the planning scene by bringup.launch.py.

Prerequisites (Terminal 1):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    python3 test_pick_screwdriver.py

Watch in RViz: the robot should move to the screwdriver, close the gripper, then lift up.
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
from moveit_msgs.msg import PlanningScene, CollisionObject
from moveit_msgs.srv import ApplyPlanningScene

sys.path.insert(0, str(Path(__file__).parent / "src" / "robot_agent"))

from robot_agent.moveit_client import MoveItClient, ARM_JOINTS
from robot_agent.gripper_client import GripperClient

# ---------------------------------------------------------------------------
# Screwdriver geometry (must match spawn_scene_objects.py)
# ---------------------------------------------------------------------------
SCREWDRIVER_X      = 0.45    # m — forward from robot base
SCREWDRIVER_Y      = 0.00    # m — centred
SCREWDRIVER_RADIUS = 0.012   # m — cylinder radius (diameter 24 mm)

# GRASP_Z is the tool0 height at the grasp point.
# The Robotiq 2F-85 finger tips are ~0.17 m below tool0 when open.
# GRASP_Z = 0.26 places the finger tips at z = 0.09 m — the screwdriver's
# centre of mass (half of height = 0.18 m).
GRASP_Z     = 0.26   # m
APPROACH_H  = 0.18   # m above GRASP_Z — gives clearance above screwdriver top
RETREAT_H   = 0.18   # m above GRASP_Z for post-grasp retreat

# Gripper pointing straight down: 180° rotation about X → (qx=1, qy=0, qz=0, qw=0)
GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW = 1.0, 0.0, 0.0, 0.0

# Home joint configuration (canonical upright rest)
HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]

# ---------------------------------------------------------------------------
# Gripper position for grasping the screwdriver
# ---------------------------------------------------------------------------
# Robotiq 2F-85: 0.0 rad → 85 mm gap (fully open)
#                0.79 rad → 0 mm gap (fully closed)
# For a 24 mm diameter screwdriver: position ≈ 0.79 × (85-24)/85 ≈ 0.57 rad
GRASP_GRIPPER_POSITION = 0.57   # rad — closes to ~24 mm, matching screwdriver diameter

# Robotiq 2F-85 gripper links (used as touch_links when attaching the screwdriver)
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
# Planning-scene helpers
# ---------------------------------------------------------------------------

def _poll_future(future, timeout_sec: float = 10.0):
    """Busy-poll a future; the background executor processes callbacks."""
    start = time.monotonic()
    while not future.done():
        if time.monotonic() - start > timeout_sec:
            return None
        time.sleep(0.05)
    return future.result()


def remove_object_from_scene(node: Node, object_id: str) -> bool:
    """Remove a collision object from MoveIt's planning scene."""
    client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    if not client.wait_for_service(timeout_sec=5.0):
        node.get_logger().error("/apply_planning_scene not available")
        return False

    scene = PlanningScene()
    scene.is_diff = True
    obj = CollisionObject()
    obj.id = object_id
    obj.header.frame_id = "world"
    obj.operation = CollisionObject.REMOVE
    scene.world.collision_objects.append(obj)

    req = ApplyPlanningScene.Request()
    req.scene = scene
    result = _poll_future(client.call_async(req), timeout_sec=5.0)
    ok = result is not None and result.success
    if ok:
        node.get_logger().info(f"Removed '{object_id}' from planning scene")
    else:
        node.get_logger().error(f"Failed to remove '{object_id}' from planning scene")
    return ok


# ---------------------------------------------------------------------------
# Standard test helpers
# ---------------------------------------------------------------------------

def check_unique_move_group(node: Node) -> bool:
    mg_nodes = [
        f"{ns}/{name}" if ns != "/" else name
        for name, ns in node.get_node_names_and_namespaces()
        if name == "move_group"
    ]
    if len(mg_nodes) > 1:
        print("\n" + "!" * 60)
        print("  ERROR: Multiple move_group nodes detected:")
        for n in mg_nodes:
            print(f"    {n}")
        print()
        print("  A stale bringup is still running. Fix:")
        print("    pkill -9 -f move_group")
        print("    pkill -9 -f ros2_control_node")
        print("    ros2 daemon stop && ros2 daemon start")
        print("  Then relaunch bringup.launch.py and re-run this test.")
        print("!" * 60 + "\n")
        return False
    return True


def wait_for_joint_states(node: Node, timeout_sec: float = 20.0) -> bool:
    received = threading.Event()

    def _cb(msg):
        received.set()

    sub = node.create_subscription(JointState, "/joint_states", _cb, 10)
    ok = received.wait(timeout_sec)
    node.destroy_subscription(sub)
    return ok


def make_pose(x: float, y: float, z: float,
              qx: float, qy: float, qz: float, qw: float) -> Pose:
    p = Pose()
    p.position.x = x
    p.position.y = y
    p.position.z = z
    p.orientation.x = qx
    p.orientation.y = qy
    p.orientation.z = qz
    p.orientation.w = qw
    return p


class PickTestNode(Node):
    def __init__(self):
        super().__init__("pick_screwdriver_test")


def ik_and_move(moveit: "MoveItClient", pose: Pose,
                velocity_scaling: float = 0.3,
                seed: list = None) -> bool:
    """Compute IK for pose, then plan+execute in joint space (fast, reliable)."""
    joints = moveit.ik(pose, seed_joints=seed)
    if joints is None:
        return False
    return moveit.plan_and_execute_joints(joints, velocity_scaling=velocity_scaling)


def main():
    rclpy.init()
    node = PickTestNode()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        # 1. Wait for controller stack
        node.get_logger().info("Waiting for /joint_states…")
        if not wait_for_joint_states(node):
            node.get_logger().error("No /joint_states after 20 s — is bringup running?")
            return

        node.get_logger().info("/joint_states OK — checking for stale nodes…")
        if not check_unique_move_group(node):
            return

        node.get_logger().info("Node check OK — connecting to MoveIt + gripper…")
        moveit = MoveItClient(node)
        gripper = GripperClient(node)
        node.get_logger().info("Clients ready.")

        # Build Cartesian waypoints
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

        steps = [
            # Step 1-3: standard pre-grasp
            ("Go to home joints",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
            ("Open gripper",
             lambda: gripper.open()),
            ("IK + move to approach pose",
             lambda: ik_and_move(moveit, approach_pose, velocity_scaling=0.3,
                                 seed=HOME_JOINTS)),

            # Step 4: allow gripper links to enter the screwdriver's collision volume.
            # The screwdriver STAYS in the scene (visible in RViz) — we are NOT removing
            # it.  We just tell MoveIt that the gripper is allowed to be in contact with
            # this specific object while planning the descent.
            ("Allow gripper↔screwdriver collision",
             lambda: moveit.allow_collision("screwdriver")),

            # Step 5: descend — fingers straddle the screwdriver (no collision error)
            ("IK + descend to grasp pose",
             lambda: ik_and_move(moveit, grasp_pose, velocity_scaling=0.2)),

            # Step 6: close to screwdriver width (not fully closed)
            ("Close gripper to screwdriver width",
             lambda: gripper.set_position(GRASP_GRIPPER_POSITION)),

            # Step 7: attach the screwdriver to the gripper so it moves with the arm
            # during retreat and remains visible in RViz.
            ("Attach screwdriver to gripper",
             lambda: moveit.attach_object("screwdriver", "tool0",
                                          touch_links=GRIPPER_LINKS)),

            # Step 8: lift up — screwdriver follows the arm
            ("IK + retreat upward",
             lambda: ik_and_move(moveit, retreat_pose, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 60)
        print("  UR5e Pick-Screwdriver Test")
        print(f"  Screwdriver at  x={SCREWDRIVER_X}, y={SCREWDRIVER_Y}")
        print(f"  Approach z      = {GRASP_Z + APPROACH_H:.3f} m")
        print(f"  Grasp z (tool0) = {GRASP_Z:.3f} m  (finger tips ≈ z={GRASP_Z - 0.17:.3f} m)")
        print("=" * 60)

        all_ok = True
        for i, (label, fn) in enumerate(steps, 1):
            print(f"\n[{i}/{len(steps)}] {label}")
            t0 = time.monotonic()
            ok = fn()
            elapsed = time.monotonic() - t0
            status = "OK" if ok else "FAILED"
            print(f"        → {status}  ({elapsed:.2f} s)")
            if not ok:
                all_ok = False
                print("        Aborting sequence.")
                break
            time.sleep(0.5)

        print("\n" + "=" * 60)
        print(f"  Result: {'SUCCESS — pick complete!' if all_ok else 'FAILED — see above.'}")
        print("=" * 60 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
