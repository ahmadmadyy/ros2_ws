#!/usr/bin/env python3
"""
Pick-and-place test for UR5e + Robotiq 2F-85.

Picks a small 50 mm box from one location and places it at another.
The box is added to the planning scene automatically at startup.

Prerequisites (Terminal 1):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    python3 test_pick_place_box.py

Watch in RViz: the robot picks the green box from the left side (clear of
the screwdriver at x=0.45, y=0) and places it on the right side.
"""

import json
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import JointState
from geometry_msgs.msg import Pose
from shape_msgs.msg import SolidPrimitive
from std_msgs.msg import ColorRGBA
from moveit_msgs.msg import (
    CollisionObject,
    ObjectColor,
    PlanningScene,
    AttachedCollisionObject,
)
from moveit_msgs.srv import ApplyPlanningScene

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "robot_agent"))

from robot_agent.moveit_client import MoveItClient, ARM_JOINTS
from robot_agent.gripper_client import GripperClient
from robot_agent.recorder.joint_recorder import JointStateRecorder

# ---------------------------------------------------------------------------
# Box geometry
# ---------------------------------------------------------------------------
BOX_SIZE = 0.05       # m — cube side length (50 mm)
BOX_HALF = BOX_SIZE / 2.0  # 0.025 m — half-height

# Pick location (left of the screwdriver, which is at x=0.45, y=0.0)
PICK_X = 0.35
PICK_Y = 0.25

# Place location (mirrored to the right)
PLACE_X = 0.35
PLACE_Y = -0.25

# Box sits on the ground plane (z = 0 surface).
BOX_CENTER_Z = BOX_HALF  # 0.025 m

# The Robotiq 2F-85 finger tips are ~0.170 m below tool0 when open.
# GRASP_Z is the tool0 height that places the finger tips at box mid-height.
FINGER_TIP_OFFSET = 0.170    # m
GRASP_Z = BOX_CENTER_Z + FINGER_TIP_OFFSET   # 0.195 m

APPROACH_H = 0.15   # m above GRASP_Z for pre/post moves
RETREAT_H  = 0.15   # m above GRASP_Z for post-move retreat

# Gripper straight down: 180° about X → (qx=1, qy=0, qz=0, qw=0)
GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW = 1.0, 0.0, 0.0, 0.0

# Gripper closing position for a 50 mm wide box.
# Robotiq 2F-85: 0.0 rad = 85 mm gap (fully open), 0.79 rad = 0 mm (fully closed).
# Linear mapping: position = 0.79 * (85 - gap_mm) / 85
GRASP_GRIPPER_POSITION = 0.79 * (85 - 50) / 85   # ≈ 0.325 rad → 50 mm gap

# Home joint configuration [shoulder_pan, lift, elbow, wrist_1, wrist_2, wrist_3]
HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]

# IK warm-start seeds for the two pick/place regions
PICK_SEED  = [0.6,  -2.2, 1.9, -1.28, -1.571, 0.0]   # shoulder rotated ~+35° for y=+0.25
PLACE_SEED = [-0.6, -2.2, 1.9, -1.28, -1.571, 0.0]   # shoulder rotated ~-35° for y=-0.25

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

OBJECT_ID = "small_box"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _poll_future(future, timeout_sec: float = 10.0):
    """Busy-poll a future while the background executor processes callbacks."""
    start = time.monotonic()
    while not future.done():
        if time.monotonic() - start > timeout_sec:
            return None
        time.sleep(0.05)
    return future.result()


def make_pose(x, y, z, qx, qy, qz, qw) -> Pose:
    p = Pose()
    p.position.x = x
    p.position.y = y
    p.position.z = z
    p.orientation.x = qx
    p.orientation.y = qy
    p.orientation.z = qz
    p.orientation.w = qw
    return p


def ik_and_move(moveit: MoveItClient, pose: Pose,
                velocity_scaling: float = 0.3,
                seeds: list = None) -> bool:
    """Try each seed in order: compute IK → plan+execute in joint space.

    Joint-space planning is more reliable than Cartesian pose planning
    because OMPL works directly in the space where the trajectory lives.
    """
    for s in (seeds or [HOME_JOINTS]):
        joints = moveit.ik(pose, seed_joints=s)
        if joints is None:
            continue
        if moveit.plan_and_execute_joints(joints, velocity_scaling=velocity_scaling):
            return True
    return False


def add_box_to_scene(node: Node) -> bool:
    """Add a small box collision object to the MoveIt planning scene."""
    client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    if not client.wait_for_service(timeout_sec=5.0):
        node.get_logger().error("/apply_planning_scene not available")
        return False

    scene = PlanningScene()
    scene.is_diff = True

    obj = CollisionObject()
    obj.id = OBJECT_ID
    obj.header.frame_id = "world"
    obj.operation = CollisionObject.ADD
    prim = SolidPrimitive()
    prim.type = SolidPrimitive.BOX
    prim.dimensions = [BOX_SIZE, BOX_SIZE, BOX_SIZE]
    obj.primitives.append(prim)
    pose = Pose()
    pose.position.x = PICK_X
    pose.position.y = PICK_Y
    pose.position.z = BOX_CENTER_Z
    pose.orientation.w = 1.0
    obj.primitive_poses.append(pose)
    scene.world.collision_objects.append(obj)

    # Colour the box green so it stands out in RViz
    col = ObjectColor()
    col.id = OBJECT_ID
    col.color = ColorRGBA(r=0.1, g=0.8, b=0.2, a=1.0)
    scene.object_colors.append(col)

    req = ApplyPlanningScene.Request()
    req.scene = scene
    result = _poll_future(client.call_async(req), timeout_sec=5.0)
    ok = result is not None and result.success
    if ok:
        node.get_logger().info(f"Added '{OBJECT_ID}' to planning scene at pick location")
    else:
        node.get_logger().error(f"Failed to add '{OBJECT_ID}' to planning scene")
    return ok


def detach_and_add_to_world(node: Node,
                             place_x: float, place_y: float,
                             place_z: float) -> bool:
    """Detach the box from the gripper and re-insert it in the world at the place pose.

    The atomic diff tells MoveIt to:
      1. Remove the object from robot attached_collision_objects
      2. Add it back to the world at the new location
    """
    client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    if not client.wait_for_service(timeout_sec=5.0):
        node.get_logger().error("/apply_planning_scene not available")
        return False

    scene = PlanningScene()
    scene.is_diff = True

    # Remove from attached objects
    aco = AttachedCollisionObject()
    aco.link_name = "tool0"
    aco.object.id = OBJECT_ID
    aco.object.operation = CollisionObject.REMOVE
    scene.robot_state.attached_collision_objects.append(aco)
    scene.robot_state.is_diff = True

    # Re-add to world at place location
    obj = CollisionObject()
    obj.id = OBJECT_ID
    obj.header.frame_id = "world"
    obj.operation = CollisionObject.ADD
    prim = SolidPrimitive()
    prim.type = SolidPrimitive.BOX
    prim.dimensions = [BOX_SIZE, BOX_SIZE, BOX_SIZE]
    obj.primitives.append(prim)
    pose = Pose()
    pose.position.x = place_x
    pose.position.y = place_y
    pose.position.z = place_z
    pose.orientation.w = 1.0
    obj.primitive_poses.append(pose)
    scene.world.collision_objects.append(obj)

    req = ApplyPlanningScene.Request()
    req.scene = scene
    result = _poll_future(client.call_async(req), timeout_sec=5.0)
    ok = result is not None and result.success
    if ok:
        node.get_logger().info(
            f"Detached '{OBJECT_ID}' — placed in world at "
            f"x={place_x:.3f}, y={place_y:.3f}, z={place_z:.3f}"
        )
    else:
        node.get_logger().error(f"Failed to detach '{OBJECT_ID}'")
    return ok


def save_trace(trace, output_dir: Path = None) -> Path:
    """Serialise an ExecutionTrace to a timestamped JSON file."""
    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "outputs" / "pick_trace"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"pick_place_box_{ts}.json"
    data = {
        "trace_id": trace.trace_id,
        "label": trace.label,
        "duration_sec": round(trace.duration_sec, 4),
        "num_snapshots": len(trace.snapshots),
        "snapshots": [
            {
                "timestamp": round(s.timestamp, 6),
                "joint_names": s.joint_names,
                "positions": [round(v, 6) for v in s.positions],
                "velocities": [round(v, 6) for v in s.velocities],
            }
            for s in trace.snapshots
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    return path


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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class PickPlaceBoxNode(Node):
    def __init__(self):
        super().__init__("pick_place_box_test")


def main():
    rclpy.init()
    node = PickPlaceBoxNode()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        # ---- startup checks ----
        node.get_logger().info("Waiting for /joint_states…")
        if not wait_for_joint_states(node):
            node.get_logger().error("No /joint_states after 20 s — is bringup running?")
            return

        node.get_logger().info("/joint_states OK — checking for stale nodes…")
        if not check_unique_move_group(node):
            return

        node.get_logger().info("Connecting to MoveIt + gripper…")
        moveit = MoveItClient(node)
        gripper = GripperClient(node)
        node.get_logger().info("Clients ready.")

        # Set up joint recorder
        recorder = JointStateRecorder(node)
        node.create_subscription(
            JointState, "/joint_states", recorder.on_joint_state, 10
        )

        # ---- define waypoints ----
        pick_approach = make_pose(PICK_X, PICK_Y, GRASP_Z + APPROACH_H,
                                  GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)
        pick_grasp    = make_pose(PICK_X, PICK_Y, GRASP_Z,
                                  GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)
        pick_retreat  = make_pose(PICK_X, PICK_Y, GRASP_Z + RETREAT_H,
                                  GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)

        place_approach = make_pose(PLACE_X, PLACE_Y, GRASP_Z + APPROACH_H,
                                   GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)
        place_grasp    = make_pose(PLACE_X, PLACE_Y, GRASP_Z,
                                   GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)
        place_retreat  = make_pose(PLACE_X, PLACE_Y, GRASP_Z + RETREAT_H,
                                   GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW)

        # ---- build step list ----
        steps = [
            # --- setup ---
            ("Add box to planning scene",
             lambda: add_box_to_scene(node)),

            # --- pick ---
            ("Go to home joints",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),

            ("Open gripper",
             lambda: gripper.open()),

            ("IK + move to pick approach",
             lambda: ik_and_move(moveit, pick_approach, velocity_scaling=0.3,
                                 seeds=[HOME_JOINTS, PICK_SEED])),

            ("Allow gripper↔box collision",
             lambda: moveit.allow_collision(OBJECT_ID)),

            ("IK + descend to grasp pose",
             lambda: ik_and_move(moveit, pick_grasp, velocity_scaling=0.2,
                                 seeds=[PICK_SEED, HOME_JOINTS])),

            (f"Close gripper to {GRASP_GRIPPER_POSITION:.3f} rad ({BOX_SIZE*1000:.0f} mm)",
             lambda: gripper.set_position(GRASP_GRIPPER_POSITION)),

            ("Attach box to gripper",
             lambda: moveit.attach_object(OBJECT_ID, "tool0", touch_links=GRIPPER_LINKS)),

            ("IK + retreat after pick",
             lambda: ik_and_move(moveit, pick_retreat, velocity_scaling=0.3,
                                 seeds=[PICK_SEED, HOME_JOINTS])),

            # --- place ---
            ("IK + move to place approach",
             lambda: ik_and_move(moveit, place_approach, velocity_scaling=0.3,
                                 seeds=[PLACE_SEED, HOME_JOINTS, PICK_SEED])),

            # Allow box↔ground_plane collision before the final descent so the
            # planner doesn't reject the target as colliding with the table.
            ("Allow box↔environment collision",
             lambda: moveit.allow_collision(OBJECT_ID)),

            ("IK + descend to place pose",
             lambda: ik_and_move(moveit, place_grasp, velocity_scaling=0.2,
                                 seeds=[PLACE_SEED, HOME_JOINTS])),

            ("Open gripper (release box)",
             lambda: gripper.open()),

            ("Detach box and update planning scene",
             lambda: detach_and_add_to_world(node, PLACE_X, PLACE_Y, BOX_CENTER_Z)),

            ("IK + retreat after place",
             lambda: ik_and_move(moveit, place_retreat, velocity_scaling=0.3,
                                 seeds=[PLACE_SEED, HOME_JOINTS])),

            ("Return to home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        # ---- banner ----
        print("\n" + "=" * 65)
        print("  UR5e Pick-and-Place Box Test")
        print(f"  Box:             {BOX_SIZE*1000:.0f} mm cube  ({OBJECT_ID})")
        print(f"  Pick location:   x={PICK_X}, y={PICK_Y}")
        print(f"  Place location:  x={PLACE_X}, y={PLACE_Y}")
        print(f"  GRASP_Z (tool0): {GRASP_Z:.3f} m")
        print(f"  Approach height: {APPROACH_H:.3f} m above grasp")
        print(f"  Gripper close:   {GRASP_GRIPPER_POSITION:.3f} rad  "
              f"(~{BOX_SIZE*1000:.0f} mm gap)")
        print("=" * 65)

        # ---- execute ----
        recorder.start_recording("pick_and_place_box")
        all_ok = True
        for i, (label, fn) in enumerate(steps, 1):
            print(f"\n[{i:02d}/{len(steps)}] {label}")
            t0 = time.monotonic()
            ok = fn()
            elapsed = time.monotonic() - t0
            status = "OK" if ok else "FAILED"
            print(f"         → {status}  ({elapsed:.2f} s)")
            if not ok:
                all_ok = False
                print("         Aborting sequence.")
                break
            time.sleep(0.3)

        trace = recorder.stop_recording()
        if trace and trace.snapshots:
            out_path = save_trace(trace)
            print(f"\n  Joint trace saved → {out_path}")
            print(f"  ({len(trace.snapshots)} snapshots, {trace.duration_sec:.2f} s)")

        print("\n" + "=" * 65)
        result_str = "SUCCESS — pick and place complete!" if all_ok else "FAILED — see above."
        print(f"  Result: {result_str}")
        print("=" * 65 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
