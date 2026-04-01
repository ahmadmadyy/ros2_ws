#!/usr/bin/env python3
"""
Continuous-screwing simulation — TOP-DOWN grasp + Cosmos/Llama trace recording.

Identical to test_screw_continuous.py but records a joint-state trace and
saves it to outputs/screw_trace/ for analysis via the robot_agent pipeline:

    POST /api/v1/traces/load?file_path=<path>  →  trace_id
    POST /api/v1/analyze/<trace_id>            →  Cosmos + Llama evaluation

Run sequence:
    Terminal 1:  ros2 launch ur5e_robotiq_moveit_config bringup.launch.py
    Terminal 2:  vllm serve nvidia/Cosmos-Reason2-2B ...
    Terminal 3:  ros2 run robot_agent robot_agent
    Terminal 4:  python3 ~/ros2_ws/tests/test_screw_continuous_cosmos.py
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

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "robot_agent"))

from robot_agent.moveit_client import MoveItClient
from robot_agent.gripper_client import GripperClient
from robot_agent.recorder.joint_recorder import JointStateRecorder

# ---------------------------------------------------------------------------
# Screwdriver / grasp geometry  (same as test_screw_continuous.py)
# ---------------------------------------------------------------------------
SCREWDRIVER_X      = 0.45
SCREWDRIVER_Y      = 0.00
SCREWDRIVER_RADIUS = 0.012

FINGER_TIP_OFFSET  = 0.170
SCREWDRIVER_MID_Z  = 0.09

GRASP_Z    = SCREWDRIVER_MID_Z + FINGER_TIP_OFFSET   # 0.26 m
APPROACH_H = 0.18
RETREAT_H  = 0.20

GRASP_QX, GRASP_QY, GRASP_QZ, GRASP_QW = 1.0, 0.0, 0.0, 0.0

GRASP_GRIPPER_POSITION = 0.57

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
SCREW_TOOL0_Z    = GRASP_Z
SCREW_APPROACH_Z = SCREW_TOOL0_Z + 0.15

# ---------------------------------------------------------------------------
# Continuous-screwing parameters
# ---------------------------------------------------------------------------
N_CYCLES = 5

WRIST3_SAFE_MIN = -(2 * math.pi - 0.3)
WRIST3_SAFE_MAX =  (2 * math.pi - 0.3)

SCREW_STEP_RAD = +(math.pi / 4)
LIFT_H         = 0.06


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
    d = current_wrist3 - initial_wrist3
    return math.cos(d / 2), -math.sin(d / 2), 0.0, 0.0


_last_joints = [None]


def ik_and_move(moveit, pose, velocity_scaling=0.3, seeds=None):
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
    for seed in seeds:
        j = moveit.ik(pose, seed_joints=seed)
        if j is not None:
            return j
    return None


def ik_and_move_with_current_wrist3(
    moveit,
    x: float,
    y: float,
    z: float,
    initial_wrist3: float,
    current_wrist3: float,
    velocity_scaling=0.3,
    seeds=None,
):
    """Move to an XYZ target while preserving the current wrist_3 roll.

    For the screwdriver return-to-holder motion, the tool only needs to remain
    top-down; forcing the canonical top-down quaternion can make IK jump to a
    distant wrapped solution after the screwing cycles. Preserving the current
    wrist_3 orientation keeps the motion on the same kinematic branch.
    """
    qx, qy, qz, qw = q_from_wrist3_delta(initial_wrist3, current_wrist3)
    pose = make_pose(x, y, z, qx, qy, qz, qw)
    return ik_and_move(moveit, pose, velocity_scaling=velocity_scaling, seeds=seeds)


def save_trace(trace) -> Path:
    out_dir = Path(__file__).parent.parent / "outputs" / "screw_trace"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"screw_continuous_{ts}.json"
    data = {
        "trace_id":       trace.trace_id,
        "label":          trace.label,
        "duration_sec":   round(trace.duration_sec, 4),
        "num_snapshots":  len(trace.snapshots),
        "snapshots": [
            {
                "timestamp":   round(s.timestamp, 6),
                "joint_names": s.joint_names,
                "positions":   [round(v, 6) for v in s.positions],
                "velocities":  [round(v, 6) for v in s.velocities],
            }
            for s in trace.snapshots
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class ScrewContinuousCosmosNode(Node):
    def __init__(self):
        super().__init__("screw_continuous_cosmos_test")


def main():
    rclpy.init()
    node = ScrewContinuousCosmosNode()

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

        # Set up joint-state recorder
        recorder = JointStateRecorder(node)
        node.create_subscription(
            JointState, "/joint_states", recorder.on_joint_state, 10
        )
        node.get_logger().info("MoveIt + gripper + recorder ready.")

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

        _engage_joints = [None]
        _initial_wrist3 = [None]

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
                        _initial_wrist3[0] = j[5]
                    return ok
            node.get_logger().error("IK failed for initial engage pose")
            return False

        def run_screw_cycles():
            initial_wrist3 = _engage_joints[0][5]
            total_cw_rad   = 0.0

            for cycle in range(1, N_CYCLES + 1):
                node.get_logger().info(
                    f"\n--- Cycle {cycle}/{N_CYCLES}  "
                    f"(start wrist_3={_engage_joints[0][5]:.3f} rad) ---"
                )

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

                ccw_joints    = list(lift_joints)
                ccw_joints[5] = WRIST3_SAFE_MIN
                if not moveit.plan_and_execute_joints(ccw_joints, velocity_scaling=0.5):
                    node.get_logger().error(f"CCW reset failed (cycle {cycle})")
                    return False

                if cycle < N_CYCLES:
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
                else:
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
            ("Return screwdriver to holder (approach)",
             lambda: ik_and_move_with_current_wrist3(
                 moveit,
                 SCREWDRIVER_X,
                 SCREWDRIVER_Y,
                 GRASP_Z + APPROACH_H,
                 _initial_wrist3[0],
                 _last_joints[0][5],
                 0.3,
                 [_last_joints[0], PICK_SEED, HOME_JOINTS],
             )),
            ("Lower to holder",
             lambda: ik_and_move_with_current_wrist3(
                 moveit,
                 SCREWDRIVER_X,
                 SCREWDRIVER_Y,
                 GRASP_Z,
                 _initial_wrist3[0],
                 _last_joints[0][5],
                 0.2,
                 [_last_joints[0], PICK_SEED],
             )),
            ("Open gripper (drop screwdriver at holder)",
             lambda: gripper.open()),
            ("Detach screwdriver from tool0",
             lambda: moveit.detach_object("screwdriver")),
            ("Retreat from holder",
             lambda: ik_and_move(moveit, retreat_pose, 0.3,
                                 [_last_joints[0], PICK_SEED, HOME_JOINTS])),
            ("Return home",
             lambda: moveit.plan_and_execute_joints(HOME_JOINTS, velocity_scaling=0.3)),
        ]

        print("\n" + "=" * 66)
        print("  Continuous Screwing + Cosmos Trace — TOP-DOWN grasp")
        print(f"  Pick   x={SCREWDRIVER_X:.2f}  y={SCREWDRIVER_Y:.2f}  "
              f"tool0-z={GRASP_Z:.3f} m")
        print(f"  Screw  x={SCREW_X:.2f}  y={SCREW_Y:.2f}  "
              f"tool0-z={SCREW_TOOL0_Z:.3f} m")
        print(f"  Cycles N={N_CYCLES},  step={math.degrees(abs(SCREW_STEP_RAD)):.0f}°/step CW")
        print("=" * 66)

        recorder.start_recording("continuous_screw_topdown")

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

        trace = recorder.stop_recording()
        if trace and trace.snapshots:
            out_path = save_trace(trace)
            print(f"\n  Joint trace saved → {out_path}")
            print(f"  ({len(trace.snapshots)} snapshots, {trace.duration_sec:.2f} s)")
            print(f"\n  Load + analyse:")
            print(f"    TRACE_FILE={out_path}")
            print(f"    TRACE_ID=$(curl -s -X POST 'http://localhost:8080/api/v1/traces/load?file_path='$TRACE_FILE | python3 -c \"import sys,json; print(json.load(sys.stdin)['trace_id'])\")")
            print(f"    (then run the analyze curl — see commands_manual.md Step 6)")
        else:
            print("\n  WARNING: no trace snapshots recorded.")

        print("\n" + "=" * 66)
        print(f"  Result: {'SUCCESS' if all_ok else 'FAILED — see above.'}")
        print("=" * 66 + "\n")
        if not all_ok:
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
