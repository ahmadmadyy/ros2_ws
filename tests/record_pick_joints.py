#!/usr/bin/env python3
"""
Interactively record joint states for each pick waypoint.

Usage:
    python3 tests/record_pick_joints.py

Jog the robot to each position using the teach pendant / MoveIt,
then press Enter to capture. Results are printed and saved to
outputs/pick_joints_<timestamp>.json.

Steps captured:
  1. home
  2. approach_screwdriver  (above screwdriver, gripper open)
  3. descend_to_grasp      (tool0 at grasp height, gripper open)
  4. close_gripper         (same arm position, gripper closed)
  5. retreat_upward        (lifted with screwdriver secured)
"""

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "robot_agent"))

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState

ARM_JOINTS = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]
GRIPPER_JOINT = "rq_robotiq_85_left_knuckle_joint"

STEPS = [
    ("home",                "Home position, gripper open."),
    ("approach_screwdriver","Above screwdriver, gripper open."),
    ("descend_to_grasp",    "At grasp height (tool0 z=0.26m), gripper open."),
    ("close_gripper",       "Gripper closed on screwdriver (arm stays put)."),
    ("retreat_upward",      "Lifted up with screwdriver secured, gripper closed."),
]


class JointCapture(Node):
    def __init__(self):
        super().__init__("joint_capture")
        self._latest = None
        self.create_subscription(JointState, "/joint_states", self._cb, 10)

    def _cb(self, msg: JointState):
        self._latest = msg

    def get(self):
        return self._latest


def main():
    rclpy.init()
    node = JointCapture()
    executor = rclpy.executors.SingleThreadedExecutor()
    executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()

    print("\n=== Pick Joint Recorder ===")
    print("Jog the robot to each position, then press Enter to capture.\n")

    # Wait for first joint state
    print("Waiting for /joint_states ...", end="", flush=True)
    while node._latest is None:
        time.sleep(0.1)
    print(" ready.\n")

    captured = []

    for phase, note in STEPS:
        print(f"Step: {phase}")
        print(f"  → {note}")
        input("  Press Enter to capture current joint state...")

        msg = node._latest
        name_to_pos = dict(zip(msg.name, msg.position))

        arm = [round(name_to_pos.get(j, 0.0), 4) for j in ARM_JOINTS]
        gripper = round(name_to_pos.get(GRIPPER_JOINT, 0.0), 4)

        wp = {
            "phase": phase,
            "joints": arm,
            "gripper": gripper,
            "velocity_scaling": 0.1 if "grasp" in phase or "close" in phase else 0.3,
            "note": note,
        }
        captured.append(wp)
        print(f"  Captured: joints={arm}  gripper={gripper}\n")

    # Save
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(__file__).parent.parent / "outputs" / f"pick_joints_{ts}.json"
    out.write_text(json.dumps(captured, indent=2))
    print(f"Saved to: {out}\n")

    print("=== Copy this into PICK_WAYPOINTS in tests/run_8b_pipeline.py ===")
    print(json.dumps(captured, indent=4))

    executor.shutdown()
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
