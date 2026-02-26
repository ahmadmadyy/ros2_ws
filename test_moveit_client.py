#!/usr/bin/env python3
"""
Standalone test for MoveItClient — moves the UR5e through a joint-space sequence.

Prerequisites (Terminal 1):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    ros2 launch ur5e_robotiq_moveit_config bringup.launch.py

Then (Terminal 2):
    cd ~/ros2_ws
    source /opt/ros/jazzy/setup.bash && source install/setup.bash
    python3 test_moveit_client.py

You should see the robot move in RViz.
"""

import math
import sys
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

sys.path.insert(0, str(Path(__file__).parent / "src" / "robot_agent"))

from robot_agent.moveit_client import MoveItClient, ARM_JOINTS

# ---------------------------------------------------------------------------
# Named joint configurations for UR5e
# (shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3) in radians
# ---------------------------------------------------------------------------
CONFIGS = {
    # Canonical upright rest
    "home":      [0.0,          -math.pi/2,  0.0,         -math.pi/2,  0.0,  0.0],
    # Arm pointing straight up
    "straight_up": [0.0,        -math.pi,    0.0,         -math.pi/2,  0.0,  0.0],
    # Elbow raised, wrist angled — looks like reaching forward
    "reach_fwd": [0.0,          -1.0,        1.5,         -2.0,         0.0,  0.0],
    # Pan 90° to the left, elbow midway
    "pan_left":  [math.pi/2,    -1.2,        1.2,         -1.5,        -math.pi/2,  0.0],
    # Pan 90° to the right
    "pan_right": [-math.pi/2,   -1.2,        1.2,         -1.5,         math.pi/2,  0.0],
    # Low pose — like pre-grasp above a table
    "pre_grasp": [0.15,         -1.2,        1.4,         -1.75,       -math.pi/2,  0.0],
}

SEQUENCE = [
    ("home",       0.3, 0.3),
    ("reach_fwd",  0.4, 0.3),
    ("pan_left",   0.3, 0.3),
    ("pan_right",  0.3, 0.3),
    ("pre_grasp",  0.3, 0.2),
    ("home",       0.4, 0.3),
]


def fmt(vals):
    return "[" + ", ".join(f"{v:+.3f}" for v in vals) + "]"


def check_unique_move_group(node: Node) -> bool:
    """Abort if more than one move_group node is visible (stale DDS endpoints)."""
    mg_nodes = [
        f"{ns}/{name}" if ns != "/" else name
        for name, ns in node.get_node_names_and_namespaces()
        if name == "move_group"  # exact match — excludes _private_* companion nodes
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


def wait_for_joint_states(node: Node, timeout_sec: float = 15.0) -> bool:
    """Block until at least one /joint_states message arrives."""
    from sensor_msgs.msg import JointState
    received = threading.Event()

    def _cb(msg):
        received.set()

    sub = node.create_subscription(JointState, "/joint_states", _cb, 10)
    ok = received.wait(timeout_sec)
    node.destroy_subscription(sub)
    return ok


class MoveItTestNode(Node):
    def __init__(self):
        super().__init__("moveit_client_test")


def main():
    rclpy.init()
    node = MoveItTestNode()

    # Spin executor in background so action clients can process callbacks
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    spin_thread = threading.Thread(target=executor.spin, daemon=True)
    spin_thread.start()

    try:
        # ------------------------------------------------------------------
        # 1. Wait for /joint_states (ensures ros2_control is up)
        # ------------------------------------------------------------------
        node.get_logger().info("Waiting for /joint_states...")
        if not wait_for_joint_states(node, timeout_sec=20.0):
            node.get_logger().error(
                "No /joint_states received after 20 s. "
                "Is bringup.launch.py running?"
            )
            return

        node.get_logger().info("/joint_states OK — checking for stale nodes...")
        if not check_unique_move_group(node):
            return

        node.get_logger().info("Node check OK — connecting to MoveIt...")

        # ------------------------------------------------------------------
        # 2. Create MoveItClient (waits for /move_group and /execute_trajectory)
        # ------------------------------------------------------------------
        client = MoveItClient(node)
        node.get_logger().info("MoveItClient ready.")

        # ------------------------------------------------------------------
        # 3. Run the move sequence
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        print(f"  UR5e MoveIt Joint-Space Test — {len(SEQUENCE)} moves")
        print(f"  Joints: {', '.join(ARM_JOINTS)}")
        print("=" * 60)

        total_ok = 0
        total_fail = 0

        for i, (name, vel, acc) in enumerate(SEQUENCE, 1):
            target = CONFIGS[name]
            print(f"\n[{i}/{len(SEQUENCE)}] Moving to '{name}'")
            print(f"  Target : {fmt(target)}")
            print(f"  Vel/Acc scaling: {vel}/{acc}")

            t0 = time.monotonic()
            ok = client.plan_and_execute_joints(target, velocity_scaling=vel, acceleration_scaling=acc)
            elapsed = time.monotonic() - t0

            status = "OK" if ok else "FAILED"
            print(f"  Result : {status}  ({elapsed:.2f} s)")

            if ok:
                total_ok += 1
            else:
                total_fail += 1

            # Brief pause between moves so you can see each pose in RViz
            time.sleep(1.0)

        # ------------------------------------------------------------------
        # 4. Summary
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        print(f"  Done: {total_ok} succeeded, {total_fail} failed")
        print("=" * 60 + "\n")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
