#!/usr/bin/env python3
"""
Mixed-model pipeline: Cosmos 2B analysis → Cosmos 8B eval → best selection → 8B waypoints.

Stages
------
  1. Run 4 Cosmos 2B analysis prompts (prompts/cosmos_*.txt) against a trace file.
     Prompts read from prompts/ (flat — no 2b/8b subdirectory).
     Outputs saved to outputs/cosmos/2b/.

  2. Evaluate those 4 outputs with Cosmos 8B using the eval prompts (prompts/eval_*.txt)
     — same prompts that were used for Llama, now run through Cosmos 8B via vLLM.
     Outputs saved to outputs/eval/8b/.

  3. Pick the evaluation with the highest overall_score.
     Print: eval output, cosmos output, trace info.

  4. Generate recommended waypoints from the best result using Cosmos 8B.
     Output saved to outputs/cosmos/8b/waypoints_*.json.

Usage
-----
    python3 tests/run_8b_pipeline.py
    python3 tests/run_8b_pipeline.py --trace outputs/screw_trace/screw_continuous_20260308_115305.json
    python3 tests/run_8b_pipeline.py --cosmos-url http://localhost:8000
"""

import argparse
import asyncio
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WS              = Path(__file__).parent.parent
PROMPTS_DIR     = WS / "prompts"          # flat — no model-size subdirectory
COSMOS_OUT      = WS / "outputs" / "cosmos" / "2b"   # analysis by 2B
EVAL_OUT        = WS / "outputs" / "eval" / "8b"     # evaluation by 8B
WAYPOINTS_OUT   = WS / "outputs" / "cosmos" / "8b"   # waypoints by 8B
SCREW_TRACE_DIR = WS / "outputs" / "screw_trace"

COSMOS_2B_URL   = "http://localhost:8000"        # 2B vLLM instance
COSMOS_8B_URL   = "http://localhost:8001"        # 8B vLLM instance
COSMOS_2B_MODEL = "nvidia/Cosmos-Reason2-2B"    # used for analysis (stage 1)
COSMOS_8B_MODEL = "nvidia/Cosmos-Reason2-8B"    # used for eval + waypoints (stages 2 & 4)
MAX_SAMPLES = 80
COSMOS_MAX_OUTPUT_TOKENS = 4096

# The 4 analysis-prompt → eval-prompt pairs
PROMPT_PAIRS = [
    ("cosmos_screw.txt",            "eval_screw.txt",            "screw"),
    ("cosmos_fault_detection.txt",  "eval_fault_detection.txt",  "fault_detection"),
    ("cosmos_energy_dynamics.txt",  "eval_energy_dynamics.txt",  "energy_dynamics"),
    ("cosmos_process_quality.txt",  "eval_process_quality.txt",  "process_quality"),
]

# ---------------------------------------------------------------------------
# Task context
# ---------------------------------------------------------------------------
TASK_CONTEXT = dict(
    task_description=(
        "Pick the screwdriver from the table using a top-down grasp, "
        "move to the screw location, and perform continuous wrist-rotation "
        "screwing cycles (CW to drive screw, lift, CCW to reposition, re-engage)"
    ),
    robot_description="UR5e 6-DOF manipulator with Robotiq 85 parallel gripper",
    screwdriver_position="x=0.45, y=0.00, z=0.09 (centre of shaft)",
    screw_position="x=0.40, y=0.10",
    grasp_z=0.26,
    grasp_gripper_position="0.57 rad (~24 mm gap)",
    grasp_quaternion="1.0, 0.0, 0.0, 0.0  (top-down, 180° about world-x)",
    n_cycles=5,
    wrist3_safe_min=-5.983,
    wrist3_safe_max=5.983,
    screw_step_rad=0.785,
)

# ---------------------------------------------------------------------------
# Hardcoded pick sequence — these are physically verified waypoints for
# picking the screwdriver at x=0.45, y=0.00, z=0.09 with a top-down grasp.
# The model never generates these; they are always prepended to its output.
# ---------------------------------------------------------------------------
PICK_WAYPOINTS = [
    {
        "phase": "home",
        "joints": [0.0, -1.57, 0.0, -1.57, 0.0, 0.0],
        "gripper": 0.0,
        "velocity_scaling": 0.3,
        "note": "Home position, gripper open.",
    },
    {
        "phase": "approach_screwdriver",
        "joints": [-0.0522, -1.5951, 0.3176, -1.5906, -0.274, -0.3265],
        "gripper": 0.0,
        "velocity_scaling": 0.3,
        "note": "Above screwdriver, gripper open.",
    },
    {
        "phase": "descend_to_grasp",
        "joints": [-0.3007, -1.7112, 1.823, -1.6815, -1.5715, -1.8709],
        "gripper": 0.0,
        "velocity_scaling": 0.1,
        "note": "Descend to grasp height (tool0 z=0.26 m) above screwdriver.",
    },
    {
        "phase": "close_gripper",
        # Arm stays at the same config as descend_to_grasp — only gripper moves.
        "joints": [-0.3007, -1.7112, 1.823, -1.6815, -1.5715, -1.8709],
        "gripper": 0.57,
        "velocity_scaling": 0.1,
        "note": "Close gripper to secure screwdriver (~24 mm gap). Arm does not move.",
    },
    {
        "phase": "retreat_upward",
        "joints": [-0.2997, -1.7153, 1.7755, -1.6297, -1.5718, -1.8706],
        "gripper": 0.57,
        "velocity_scaling": 0.3,
        "note": "Retreat upward with screwdriver secured.",
    },
]

# Joint state handed off to the model — must match the last PICK_WAYPOINTS entry.
_HANDOFF_JOINTS = PICK_WAYPOINTS[-1]["joints"]

# ---------------------------------------------------------------------------
# Waypoint generation prompt — parameterised output
# ---------------------------------------------------------------------------
WAYPOINT_PROMPT_TEMPLATE = """You are an expert motion planner for a UR5e robotic arm with a Robotiq 85 gripper.

## Task
{task_description}

## Robot Setup
- Robot: {robot_description}
- Screw target position: {screw_position}
- Wrist_3 safe range: [{wrist3_safe_min}, {wrist3_safe_max}] rad

## Reference Screwing Sequence (current implementation — scored {eval_overall_score}/5.0)
The current implementation uses the following pattern. Your task is to output IMPROVED
parameters that address the evaluation feedback.

### Key Joint Configurations (from actual execution, IK-verified)
Engage position (tool0 at screw, z=0.26 m, top-down orientation):
  {engage_joints}

Lift position (raised {current_lift_height_cm:.0f} cm above engage for CCW repositioning):
  {lift_joints}

### Current Parameters
- n_cycles: {n_cycles}
- cw_step_rad: {screw_step_rad}   (CW rotation per step, positive = clockwise from above)
- cw_velocity_scaling: 0.2
- lift_height_m: 0.06
- lift_velocity_scaling: 0.3
- ccw_velocity_scaling: 0.5
- lower_velocity_scaling: 0.15
- final_lift_velocity_scaling: 0.3
- return_home_velocity_scaling: 0.3

### Per-Cycle Pattern (this structure is fixed)
1. CW rotation (engaged with screw — drives the screw in):
   - From engage joints, step j6 by cw_step_rad each step.
   - Continue until j6 reaches {wrist3_safe_max} rad.
   - ONLY j6 changes per step; j1-j5 are locked to engage values.
2. Lift (disengage tip before CCW reset):
   - Move to lift joints (j1-j5 change); j6 stays at its CW-end value.
3. CCW reposition (while lifted — does NOT unscrew):
   - Set ONLY j6 = {wrist3_safe_min} rad; j1-j5 stay at lift values.
4. Lower to re-engage:
   - Move back to engage joints; j6 stays at {wrist3_safe_min} rad.
   (Last cycle: final lift instead of lower, then open gripper, then home.)

## Trajectory Analysis (from Cosmos 2B)
{cosmos_analysis_text}

## Evaluation Feedback
Score: {eval_overall_score}/5.0
Suggestions for improvement:
{eval_suggestions}

## Your Task
Based on the evaluation feedback, generate an IMPROVED parameter set.
You may adjust velocities, CW step size, number of cycles, and lift height.
You may also adjust the engage/lift joint values (j1-j5) if you believe a different
approach angle or height would help, but keep them close to the reference unless
you have a specific reason.

Output ONLY a JSON object in this exact format:
```json
{{
  "task_summary": "<brief description of what was improved>",
  "improvements_addressed": ["<specific improvement and why>"],
  "screwing_parameters": {{
    "n_cycles": <int, 1-10>,
    "cw_step_rad": <float, positive for CW, e.g. 0.785>,
    "cw_velocity_scaling": <float, 0.05-1.0>,
    "lift_height_m": <float, 0.02-0.15>,
    "lift_velocity_scaling": <float, 0.05-1.0>,
    "ccw_velocity_scaling": <float, 0.05-1.0>,
    "lower_velocity_scaling": <float, 0.05-1.0>,
    "final_lift_velocity_scaling": <float, 0.05-1.0>,
    "return_home_velocity_scaling": <float, 0.05-1.0>
  }},
  "engage_joints": [j1, j2, j3, j4, j5, j6],
  "lift_joints": [j1, j2, j3, j4, j5, j6]
}}
```

IMPORTANT constraints:
- j1 (shoulder_pan): [-6.283, 6.283],  j2 (shoulder_lift): [-6.283, 6.283]
- j3 (elbow): [-3.142, 3.142],  j4-j6: [-6.283, 6.283]
- engage_joints and lift_joints j1-j5 must remain IK-valid for the screw location
- Keep j1-j5 close to the reference values unless evaluation specifically calls for a different approach
- j6 in engage_joints/lift_joints is the starting wrist_3 — typically near the reference value
- cw_step_rad must be positive (clockwise from above with top-down grasp)
- The code generates all individual CW step waypoints from these parameters automatically
"""


# ---------------------------------------------------------------------------
# Reference extraction — pull engage/lift joints from trace
# ---------------------------------------------------------------------------

def extract_screw_reference(data: dict) -> dict | None:
    """Extract engage and lift joint configurations from the recorded trace.

    Strategy:
      1. Find when the gripper closes (gripper > 0.5) — marks pick complete.
      2. After gripper close, find the first sustained CW wrist_3 increase
         (the actual screwing, not the pick rotation).
      3. The joints at that point are the *engage* configuration.
      4. After the CW block, find where j0-j4 change (= the lift).
    """
    snaps = data["snapshots"]
    if not snaps:
        return None

    joint_names = snaps[0]["joint_names"]
    arm_keys = [
        "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
        "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
    ]
    idx = {n: i for i, n in enumerate(joint_names)}
    grip_idx = idx["rq_robotiq_85_left_knuckle_joint"]

    arm = np.array([[s["positions"][idx[k]] for k in arm_keys] for s in snaps])
    grip = np.array([s["positions"][grip_idx] for s in snaps])
    w3 = arm[:, 5]

    # 1. Find gripper close
    grip_close = int(np.argmax(grip > 0.5))
    if grip_close == 0 and grip[0] <= 0.5:
        return None  # gripper never closes

    # 2. After gripper close, find first sustained CW block using windowed delta
    # With top-down grasp, CW from above = j6 INCREASING (positive delta)
    window = 100
    cw_start = None
    for i in range(grip_close, len(w3) - window):
        if w3[i + window] - w3[i] > 0.5:   # positive = CW from above
            cw_start = i
            break
    if cw_start is None:
        return None

    engage = [round(float(v), 4) for v in arm[cw_start]]

    # 3. Find end of first CW block: where w3 stops increasing over a window
    cw_end = cw_start
    for i in range(cw_start + window, len(w3) - window):
        if w3[i + window] - w3[i] < -0.5:   # CCW = lift phase starting
            cw_end = i
            break

    # 4. Walk forward from CCW start to find where j0-j4 first change (= lift)
    #    Then read the stabilised lift joints a bit further ahead.
    lift_idx = cw_end
    cw_end_j04 = arm[cw_end, :5]
    for i in range(cw_end, min(cw_end + 500, len(arm))):
        if np.max(np.abs(arm[i, :5] - cw_end_j04)) > 0.02:
            lift_idx = i
            break

    # Let it stabilise
    for i in range(lift_idx, min(lift_idx + 200, len(arm) - 1)):
        if np.max(np.abs(arm[i + 1, :5] - arm[i, :5])) < 0.002:
            lift_idx = i
            break

    lift = [round(float(v), 4) for v in arm[lift_idx]]

    return {"engage_joints": engage, "lift_joints": lift}


# ---------------------------------------------------------------------------
# Generate waypoints from model parameters + reference joints
# ---------------------------------------------------------------------------

def generate_waypoints_from_params(
    params: dict,
    engage_joints: list[float],
    lift_joints: list[float],
) -> list[dict]:
    """Build the full screwing waypoint sequence from model-chosen parameters.

    The structure mirrors test_screw_continuous.py exactly:
      Per cycle: CW steps → lift → CCW reposition → lower (or final lift on last).
      After all cycles: open gripper → return home.
    """
    n_cycles   = int(params.get("n_cycles", 5))
    cw_step    = float(params.get("cw_step_rad", 0.785))   # positive = CW from above
    cw_vel     = float(params.get("cw_velocity_scaling", 0.2))
    lift_vel   = float(params.get("lift_velocity_scaling", 0.3))
    ccw_vel    = float(params.get("ccw_velocity_scaling", 0.5))
    lower_vel  = float(params.get("lower_velocity_scaling", 0.15))
    final_vel  = float(params.get("final_lift_velocity_scaling", 0.3))
    home_vel   = float(params.get("return_home_velocity_scaling", 0.3))

    w3_min = TASK_CONTEXT["wrist3_safe_min"]
    w3_max = TASK_CONTEXT["wrist3_safe_max"]

    waypoints: list[dict] = []
    cur_engage = list(engage_joints)

    for cyc in range(1, n_cycles + 1):
        # (a) CW rotation — only j6 changes
        joints = list(cur_engage)
        step_n = 0
        while joints[5] + cw_step <= w3_max:
            joints[5] += cw_step
            step_n += 1
            waypoints.append({
                "phase": f"CW step {step_n} (cycle {cyc})",
                "joints": [round(j, 4) for j in joints],
                "gripper": 0.57,
                "velocity_scaling": cw_vel,
                "note": f"CW rotation, wrist_3={joints[5]:.3f} rad",
            })
        cw_end_w3 = joints[5]

        # (b) Lift — j0-j4 change, j6 preserved
        lj = list(lift_joints)
        lj[5] = cw_end_w3
        waypoints.append({
            "phase": f"Lift (cycle {cyc})",
            "joints": [round(j, 4) for j in lj],
            "gripper": 0.57,
            "velocity_scaling": lift_vel,
            "note": f"Lift to disengage, wrist_3 preserved at {cw_end_w3:.3f} rad",
        })

        # (c) CCW reposition — only j6 changes
        ccw = list(lj)
        ccw[5] = w3_min
        waypoints.append({
            "phase": f"CCW reposition (cycle {cyc})",
            "joints": [round(j, 4) for j in ccw],
            "gripper": 0.57,
            "velocity_scaling": ccw_vel,
            "note": f"CCW reset j6 to {w3_min:.3f} rad",
        })

        # (d) Lower / final lift
        if cyc < n_cycles:
            lo = list(cur_engage)
            lo[5] = w3_min
            waypoints.append({
                "phase": f"Lower to re-engage (cycle {cyc})",
                "joints": [round(j, 4) for j in lo],
                "gripper": 0.57,
                "velocity_scaling": lower_vel,
                "note": f"Lower back to engage, wrist_3={w3_min:.3f} rad",
            })
            cur_engage = list(lo)
        else:
            waypoints.append({
                "phase": "Final lift",
                "joints": [round(j, 4) for j in ccw],
                "gripper": 0.57,
                "velocity_scaling": final_vel,
                "note": "Final lift after last cycle",
            })

    # Open gripper
    waypoints.append({
        "phase": "Open gripper",
        "joints": waypoints[-1]["joints"],
        "gripper": 0.0,
        "velocity_scaling": 0.1,
        "note": "Release screwdriver",
    })

    # Return home
    waypoints.append({
        "phase": "Return home",
        "joints": [0.0, -1.5708, 0.0, -1.5708, 0.0, 0.0],
        "gripper": 0.0,
        "velocity_scaling": home_vel,
        "note": "Return to home position",
    })

    return waypoints


# ---------------------------------------------------------------------------
# Trajectory helpers (copied from run_cosmos_4prompts.py style)
# ---------------------------------------------------------------------------

def find_default_trace() -> Path:
    specific = SCREW_TRACE_DIR / "screw_continuous_20260308_115305.json"
    if specific.exists():
        return specific
    candidates = sorted(SCREW_TRACE_DIR.glob("screw_continuous_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No screw_continuous_*.json in {SCREW_TRACE_DIR}")
    return candidates[0]


def sample_trace(data: dict) -> list[dict]:
    snaps = data["snapshots"]
    n = len(snaps)
    if n <= MAX_SAMPLES:
        return snaps
    indices = [round(i * (n - 1) / (MAX_SAMPLES - 1)) for i in range(MAX_SAMPLES)]
    seen, result = set(), []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            result.append(snaps[idx])
    return result


def build_trajectory_json(data: dict) -> str:
    sampled = sample_trace(data)
    t0 = data["snapshots"][0]["timestamp"]
    rows = []
    for s in sampled:
        joint_names = s["joint_names"]
        positions   = s["positions"]
        arm_idx = {n: i for i, n in enumerate(joint_names)}
        arm_joints = [
            round(positions[arm_idx["shoulder_pan_joint"]],   4),
            round(positions[arm_idx["shoulder_lift_joint"]],  4),
            round(positions[arm_idx["elbow_joint"]],          4),
            round(positions[arm_idx["wrist_1_joint"]],        4),
            round(positions[arm_idx["wrist_2_joint"]],        4),
            round(positions[arm_idx["wrist_3_joint"]],        4),
        ]
        gripper = round(
            positions[arm_idx["rq_robotiq_85_left_knuckle_joint"]], 4
        )
        rows.append({
            "timestamp": round(s["timestamp"] - t0, 3),
            "joints":    arm_joints,
            "gripper_state": gripper,
        })
    return json.dumps(rows, separators=(",", ":"))


def compute_trajectory_metrics(trajectory_json: str) -> dict[str, str]:
    """Pre-compute trajectory metrics from the sampled JSON for prompt injection."""
    rows = json.loads(trajectory_json)
    timestamps = [r["timestamp"] for r in rows]
    joints = np.array([r["joints"] for r in rows])
    grippers = np.array([r["gripper_state"] for r in rows])
    total_duration = timestamps[-1] - timestamps[0]

    dt = np.diff(timestamps)
    dj = np.diff(joints, axis=0)
    vel = dj / dt[:, None]
    peak_vel = np.max(np.abs(vel), axis=0)

    w3 = joints[:, 5]
    dw3 = np.diff(w3)
    # CW (positive, j6 increasing) = screwing direction; CCW (negative) = reposition
    # With top-down grasp, j6 increasing = CW from above = drives screw in
    cw_mask  = dw3 > 0.01
    ccw_mask = dw3 < -0.01
    total_cw_rad  = float(np.sum(np.abs(dw3[cw_mask])))   # CW = screwing strokes
    total_ccw_rad = float(np.sum(np.abs(dw3[ccw_mask])))  # CCW = repositioning
    total_travel  = total_cw_rad + total_ccw_rad
    screw_eff = total_cw_rad / total_travel * 100 if total_travel > 0 else 0

    w3_range = TASK_CONTEXT["wrist3_safe_max"] - TASK_CONTEXT["wrist3_safe_min"]
    expected_cw_rad = TASK_CONTEXT["n_cycles"] * w3_range
    expected_cw_deg = math.degrees(expected_cw_rad)

    # Find CW (screwing) blocks — j6 increasing = CW from above
    in_cw = False
    cw_blocks = []
    block_start = None
    for i in range(len(dw3)):
        if dw3[i] > 0.01:   # CW = screwing (j6 increasing)
            if not in_cw:
                block_start = i
                in_cw = True
        else:
            if in_cw:
                cw_blocks.append((block_start, i))
                in_cw = False
    if in_cw:
        cw_blocks.append((block_start, len(dw3)))

    # Filter to real screwing blocks
    all_cw_blocks = list(cw_blocks)
    cw_blocks_filtered = []
    for start, end in all_cw_blocks:
        j15 = joints[start:end + 1, :5]
        drift = np.max(np.abs(j15 - j15[0]), axis=0)
        if np.max(drift) < 0.5:
            cw_blocks_filtered.append((start, end))
    screw_blocks = cw_blocks_filtered if cw_blocks_filtered else all_cw_blocks

    drift_strs = []
    durations = []
    for start, end in screw_blocks:
        j15 = joints[start:end + 1, :5]
        drift = np.max(np.abs(j15 - j15[0]), axis=0)
        max_drift = float(np.max(drift))
        dur = timestamps[end] - timestamps[start]
        durations.append(round(dur, 1))
        drift_strs.append(f"max={max_drift:.4f} rad")

    if len(durations) > 1:
        mean_dur = np.mean(durations)
        std_dur  = np.std(durations)
        cov = std_dur / mean_dur * 100 if mean_dur > 0 else 0
    else:
        cov = 0.0

    cw_time = sum(timestamps[e] - timestamps[s] for s, e in screw_blocks)
    productive_pct = cw_time / total_duration * 100 if total_duration > 0 else 0

    idle_mask = np.all(np.abs(dj) < 0.001, axis=1)
    idle_time = float(np.sum(dt[idle_mask]))
    idle_pct  = idle_time / total_duration * 100 if total_duration > 0 else 0

    grip_closed = np.where(grippers > 0.5)[0]
    gripper_var = 0.0
    if len(grip_closed) > 0:
        g_screw = grippers[grip_closed[0]:]
        gripper_var = float(np.max(g_screw) - np.min(g_screw))

    home_error = np.abs(joints[-1] - joints[0])
    home_l2 = float(np.linalg.norm(home_error))

    engage_rep_str = "N/A"
    engage_max_dev = None
    if len(screw_blocks) > 1:
        engage_pos = np.array([joints[s, :5] for s, e in screw_blocks])
        engage_max_dev = np.max(np.abs(engage_pos - engage_pos.mean(axis=0)), axis=0)
        engage_rep_str = f"[{', '.join(f'{v:.4f}' for v in engage_max_dev)}] rad (worst: {engage_max_dev.max():.4f})"

    # Build scoring constraints
    constraints = []
    worst_drift = max((float(d.split("=")[1].split(" ")[0]) for d in drift_strs), default=0)
    if worst_drift > 0.1:
        constraints.append(f"- CW Stroke Quality should score 2: joint drift during CW strokes reached {worst_drift:.4f} rad (> 0.1 rad)")
    elif worst_drift > 0.02:
        constraints.append(f"- CW Stroke Quality should score 3: joint drift during CW strokes reached {worst_drift:.4f} rad (0.02–0.1 rad)")
    elif worst_drift > 0.005:
        constraints.append(f"- CW Stroke Quality should score 4: joint drift during CW strokes reached {worst_drift:.4f} rad (0.005–0.02 rad)")

    if cov > 40:
        constraints.append(f"- Cycle Completion should score at most 2: CW duration CoV is {cov:.1f}% (> 40%)")
    elif cov > 15:
        constraints.append(f"- Cycle Completion should score at most 3: CW duration CoV is {cov:.1f}% (15–40%)")
    elif cov > 5:
        constraints.append(f"- Cycle Completion should score at most 4: CW duration CoV is {cov:.1f}% (5–15%)")

    if screw_eff < 45:
        constraints.append(f"- Efficiency should score 2: screwing efficiency ratio is {screw_eff:.1f}% (< 45%)")
    elif screw_eff <= 55:
        constraints.append(f"- Efficiency should score 3: screwing efficiency ratio is {screw_eff:.1f}% (45–55%)")
    elif screw_eff <= 70:
        constraints.append(f"- Efficiency should score 4: screwing efficiency ratio is {screw_eff:.1f}% (55–70%)")

    pw3v = float(peak_vel[5])
    if pw3v > 2.0:
        constraints.append(f"- Smoothness should score at most 2: peak wrist_3 velocity is {pw3v:.2f} rad/s (> 2.0)")
    elif pw3v > 1.0:
        constraints.append(f"- Smoothness should score at most 3: peak wrist_3 velocity is {pw3v:.2f} rad/s (1.0–2.0)")
    elif pw3v > 0.5:
        constraints.append(f"- Smoothness should score at most 4: peak wrist_3 velocity is {pw3v:.2f} rad/s (0.5–1.0)")

    if engage_max_dev is not None:
        worst_rep = float(engage_max_dev.max())
        if worst_rep > 0.1:
            constraints.append(f"- Screw Engagement should score at most 2: engagement repeatability worst deviation is {worst_rep:.4f} rad (> 0.1)")
        elif worst_rep > 0.02:
            constraints.append(f"- Screw Engagement should score at most 3: engagement repeatability worst deviation is {worst_rep:.4f} rad (0.02–0.1)")
        elif worst_rep > 0.005:
            constraints.append(f"- Screw Engagement should score at most 4: engagement repeatability worst deviation is {worst_rep:.4f} rad (0.005–0.02)")

    cw_error_pct = abs(math.degrees(total_cw_rad) - expected_cw_deg) / expected_cw_deg * 100 if expected_cw_deg > 0 else 0
    if cw_error_pct > 30:
        constraints.append(f"- Cycle Completion rotation accuracy should score at most 2: actual CW is {cw_error_pct:.1f}% off expected")
    elif cw_error_pct > 15:
        constraints.append(f"- Cycle Completion rotation accuracy should score at most 3: actual CW is {cw_error_pct:.1f}% off expected")
    elif cw_error_pct > 5:
        constraints.append(f"- Cycle Completion rotation accuracy should score at most 4: actual CW is {cw_error_pct:.1f}% off expected")

    if home_l2 > 0.05:
        constraints.append(f"- Return-to-home error indicates score 2: L2={home_l2:.4f} rad")
    elif home_l2 > 0.02:
        constraints.append(f"- Return-to-home error indicates score 3: L2={home_l2:.4f} rad")
    elif home_l2 > 0.005:
        constraints.append(f"- Return-to-home error indicates score 4: L2={home_l2:.4f} rad")

    if not constraints:
        constraints.append("- All metrics fall within Score 5 thresholds.")

    return {
        "metrics_scoring_constraints": "\n".join(constraints),
        "metrics_total_cw_deg":         f"{math.degrees(total_cw_rad):.1f}",
        "metrics_expected_cw_deg":      f"{expected_cw_deg:.1f}",
        "metrics_total_ccw_deg":        f"{math.degrees(total_ccw_rad):.1f}",
        "metrics_screw_efficiency":     f"{screw_eff:.1f}",
        "metrics_peak_w3_vel":          f"{peak_vel[5]:.4f}",
        "metrics_peak_velocities":      f"[{', '.join(f'{v:.4f}' for v in peak_vel)}]",
        "metrics_cw_drift_per_block":   "; ".join(f"block {i+1}: {d}" for i, d in enumerate(drift_strs)),
        "metrics_cw_durations":         str(durations),
        "metrics_cw_duration_cov":      f"{cov:.1f}",
        "metrics_productive_time":      f"{cw_time:.1f}",
        "metrics_productive_pct":       f"{productive_pct:.1f}",
        "metrics_total_duration":       f"{total_duration:.1f}",
        "metrics_idle_pct":             f"{idle_pct:.1f}",
        "metrics_gripper_var":          f"{gripper_var:.4f}",
        "metrics_home_error_l2":        f"{home_l2:.4f}",
        "metrics_home_error_per_joint": f"[{', '.join(f'{v:.4f}' for v in home_error)}]",
        "metrics_w3_min":               f"{w3.min():.4f}",
        "metrics_w3_max":               f"{w3.max():.4f}",
        "metrics_engage_repeatability": engage_rep_str,
    }


def fill_analysis_prompt(template: str, trajectory_json: str, metrics: dict[str, str]) -> str:
    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_reasoning_text"] = "N/A — this is the initial trajectory analysis."
    ctx["joint_trajectory_json"] = trajectory_json
    ctx.update(metrics)
    result = template
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    return result


def fill_eval_prompt(
    template: str,
    cosmos_reasoning: str,
    trajectory_json: str,
    metrics: dict[str, str] | None = None,
) -> str:
    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_reasoning_text"] = cosmos_reasoning
    ctx["joint_trajectory_json"] = trajectory_json
    if metrics:
        ctx.update(metrics)
    result = template
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    return result


def fill_waypoint_prompt(
    cosmos_analysis_text: str,
    eval_data: dict,
    screw_ref: dict,
) -> str:
    suggestions = eval_data.get("suggestions", [])
    if isinstance(suggestions, list):
        suggestions_str = "\n".join(f"- {s}" for s in suggestions)
    else:
        suggestions_str = str(suggestions)
    overall_score = eval_data.get("overall_score", "N/A")

    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_analysis_text"] = cosmos_analysis_text
    ctx["eval_overall_score"] = overall_score
    ctx["eval_suggestions"] = suggestions_str
    ctx["engage_joints"] = screw_ref["engage_joints"]
    ctx["lift_joints"] = screw_ref["lift_joints"]
    ctx["current_lift_height_cm"] = 6.0  # default 6 cm

    result = WAYPOINT_PROMPT_TEMPLATE
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    return result


def _repair_json(s: str) -> str:
    """Fix common model JSON errors: missing commas between string items."""
    # Insert missing comma between adjacent quoted strings: "..." "..."  →  "...", "..."
    s = re.sub(r'"\s*\n(\s*)"', '",\n\\1"', s)
    return s


def parse_json_from_response(text: str) -> dict:
    for pattern in (r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", r"(\{[\s\S]+\})"):
        match = re.search(pattern, text)
        if match:
            raw = match.group(1)
            for candidate in (raw, _repair_json(raw)):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
    return {"parse_error": "Could not extract JSON", "raw_response": text[:2000]}


# ---------------------------------------------------------------------------
# Cosmos API
# ---------------------------------------------------------------------------

async def call_cosmos(
    client: httpx.AsyncClient,
    prompt: str,
    cosmos_url: str,
    model: str,
    max_tokens: int = 4096,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": max_tokens,
        "repetition_penalty": 1.1,
    }
    resp = await client.post(f"{cosmos_url}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def main(trace_path: Path, cosmos_2b_url: str = COSMOS_2B_URL, cosmos_8b_url: str = COSMOS_8B_URL):
    COSMOS_OUT.mkdir(parents=True, exist_ok=True)
    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    WAYPOINTS_OUT.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    context_limit = 16384

    # ── 0. Check vLLM ─────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=600.0) as client:
        for url, model in ((COSMOS_2B_URL, COSMOS_2B_MODEL), (COSMOS_8B_URL, COSMOS_8B_MODEL)):
            try:
                r = await client.get(f"{url}/v1/models", timeout=5.0)
                served = [m["id"] for m in r.json().get("data", [])]
                print(f"[0] {url} — served: {served}")
                if model not in served:
                    print(f"    WARNING: {model} not in served models: {served}")
                    return
            except Exception as e:
                print(f"[0] ERROR: vLLM not reachable at {url} — {e}")
                return

        # ── 1. Load trace ──────────────────────────────────────────────────
        print(f"\n[1] Loading trace: {trace_path.name}")
        with open(trace_path) as f:
            data = json.load(f)
        trace_id = data.get("trace_id", "unknown")
        print(f"    trace_id={trace_id}  snapshots={data['num_snapshots']}  "
              f"duration={data['duration_sec']:.1f}s")

        trajectory_json = build_trajectory_json(data)
        print(f"    Sampled to {MAX_SAMPLES} waypoints")

        metrics = compute_trajectory_metrics(trajectory_json)
        print(f"    Metrics: CW={metrics['metrics_total_cw_deg']}° "
              f"(expected {metrics['metrics_expected_cw_deg']}°), "
              f"efficiency={metrics['metrics_screw_efficiency']}%, "
              f"CW_CoV={metrics['metrics_cw_duration_cov']}%")

        # ── 2. Stage 1: Cosmos 2B analysis ────────────────────────────────
        print(f"\n[2] Stage 1 — Cosmos 2B analysis ({len(PROMPT_PAIRS)} prompts)")
        print(f"    Model:        {COSMOS_2B_MODEL}")
        print(f"    Prompts from: prompts/")
        print(f"    Output to:    outputs/cosmos/2b/\n")
        print(f"    {'Slug':<25} {'Time':>6}  {'Chars':>6}  File")
        print(f"    {'─'*25} {'─'*6}  {'─'*6}  {'─'*50}")

        analysis_outputs: dict[str, dict] = {}  # slug → {response, file, ...}

        for cosmos_prompt_file, _, slug in PROMPT_PAIRS:
            template = (PROMPTS_DIR / cosmos_prompt_file).read_text()
            filled = fill_analysis_prompt(template, trajectory_json, metrics)

            est_tokens = int(len(filled) / 2.5)
            max_tokens = min(COSMOS_MAX_OUTPUT_TOKENS, context_limit - est_tokens - 200)
            if max_tokens < 1024:
                print(f"    {slug:<25}  SKIP  prompt too large (~{est_tokens} tokens)")
                continue

            t0 = time.time()
            response = None
            for attempt in range(3):
                try:
                    response = await call_cosmos(client, filled, COSMOS_2B_URL, COSMOS_2B_MODEL, max_tokens)
                    break
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(3)
                    else:
                        elapsed = time.time() - t0
                        detail = ""
                        if isinstance(e, httpx.HTTPStatusError):
                            detail = f" | {e.response.text[:300]}"
                        print(f"    {slug:<25} {elapsed:>5.1f}s   ERR   FAILED: {e}{detail}")
            if response is None:
                continue
            elapsed = time.time() - t0

            out_txt  = COSMOS_OUT / f"cosmos_{slug}_{trace_id}_{ts}.txt"
            out_json = COSMOS_OUT / f"cosmos_{slug}_{trace_id}_{ts}.json"
            out_txt.write_text(response)

            meta = {
                "prompt_file":  cosmos_prompt_file,
                "prompt_slug":  slug,
                "model":        COSMOS_2B_MODEL,
                "trace_id":     trace_id,
                "trace_file":   str(trace_path),
                "timestamp":    ts,
                "elapsed_sec":  round(elapsed, 2),
                "response":     response,
            }
            out_json.write_text(json.dumps(meta, indent=2))

            analysis_outputs[slug] = {
                "response":  response,
                "txt_file":  out_txt,
                "json_file": out_json,
                "meta":      meta,
            }
            print(f"    {slug:<25} {elapsed:>5.1f}s  {len(response):>6}  {out_txt.name}")

        if not analysis_outputs:
            print("\n    No analysis outputs — aborting.")
            return

        # ── 3. Stage 2: Cosmos 8B evaluation ──────────────────────────────
        print(f"\n[3] Stage 2 — Cosmos 8B evaluation ({len(analysis_outputs)} prompts)")
        print(f"    Model:             {COSMOS_8B_MODEL}")
        print(f"    Eval prompts from: prompts/")
        print(f"    Output to:         outputs/eval/8b/\n")
        print(f"    {'Slug':<25} {'Time':>6}  {'Score':>6}  File")
        print(f"    {'─'*25} {'─'*6}  {'─'*6}  {'─'*50}")

        eval_outputs: dict[str, dict] = {}  # slug → {score, eval_data, file, ...}

        for _, eval_prompt_file, slug in PROMPT_PAIRS:
            if slug not in analysis_outputs:
                print(f"    {slug:<25}  SKIP  no cosmos analysis output")
                continue

            cosmos_reasoning = analysis_outputs[slug]["response"]
            eval_template = (PROMPTS_DIR / eval_prompt_file).read_text()
            filled = fill_eval_prompt(eval_template, cosmos_reasoning, trajectory_json, metrics)

            est_tokens = int(len(filled) / 2.5)
            max_tokens = min(COSMOS_MAX_OUTPUT_TOKENS, context_limit - est_tokens - 200)
            if max_tokens < 1024:
                print(f"    {slug:<25}  SKIP  eval prompt too large (~{est_tokens} tokens)")
                continue

            t0 = time.time()
            try:
                response = await call_cosmos(client, filled, COSMOS_8B_URL, COSMOS_8B_MODEL, max_tokens)
                elapsed = time.time() - t0
            except Exception as e:
                elapsed = time.time() - t0
                detail = ""
                if isinstance(e, httpx.HTTPStatusError):
                    detail = f" | {e.response.text[:300]}"
                print(f"    {slug:<25} {elapsed:>5.1f}s   ERR   FAILED: {e}{detail}")
                continue

            eval_data = parse_json_from_response(response)
            eval_data["_meta"] = {
                "eval_prompt":      eval_prompt_file,
                "slug":             slug,
                "trace_id":         trace_id,
                "trace_file":       str(trace_path),
                "cosmos_file":      str(analysis_outputs[slug]["json_file"]),
                "analysis_model":   COSMOS_2B_MODEL,
                "eval_model":       COSMOS_8B_MODEL,
                "timestamp":      ts,
                "elapsed_sec":    round(elapsed, 2),
                "raw_response":   response,
            }

            out_file = EVAL_OUT / f"eval_{slug}_{trace_id}_{ts}.json"
            out_file.write_text(json.dumps(eval_data, indent=2))

            overall = eval_data.get("overall_score", None)
            score_str = f"{overall:.2f}" if isinstance(overall, (int, float)) else str(overall)
            print(f"    {slug:<25} {elapsed:>5.1f}s  {score_str:>6}  {out_file.name}")

            eval_outputs[slug] = {
                "score":     overall,
                "eval_data": eval_data,
                "eval_file": out_file,
                "cosmos_file": analysis_outputs[slug]["json_file"],
                "cosmos_response": analysis_outputs[slug]["response"],
            }

        if not eval_outputs:
            print("\n    No evaluation outputs — aborting.")
            return

        # ── 4. Stage 3: Pick best evaluation ──────────────────────────────
        print(f"\n[4] Stage 3 — Selecting best evaluation")

        valid = {
            slug: info for slug, info in eval_outputs.items()
            if isinstance(info["score"], (int, float))
        }
        if not valid:
            print("    No valid scores found — using first available.")
            best_slug = next(iter(eval_outputs))
        else:
            best_slug = max(valid, key=lambda s: valid[s]["score"])

        best = eval_outputs[best_slug]
        best_score = best["score"]
        score_str = f"{best_score:.2f}" if isinstance(best_score, (int, float)) else str(best_score)

        print(f"\n    ┌─────────────────────────────────────────────────────┐")
        print(f"    │  Best evaluation: {best_slug:<20}  score={score_str}/5.0  │")
        print(f"    └─────────────────────────────────────────────────────┘")

        print(f"\n    All scores:")
        for slug, info in eval_outputs.items():
            sc = info["score"]
            sc_str = f"{sc:.2f}" if isinstance(sc, (int, float)) else str(sc)
            marker = " ← BEST" if slug == best_slug else ""
            print(f"      {slug:<25}  {sc_str:>6}{marker}")

        print(f"\n    Eval output file:   {best['eval_file']}")
        print(f"    Cosmos output file: {best['cosmos_file']}")
        print(f"    Trace file:         {trace_path}")

        # Print critical failures and suggestions
        eval_data = best["eval_data"]
        if eval_data.get("critical_failures"):
            print(f"\n    Critical failures:")
            for f in eval_data["critical_failures"]:
                print(f"      - {f}")
        if eval_data.get("suggestions"):
            print(f"\n    Suggestions for improvement:")
            for s in eval_data["suggestions"]:
                print(f"      - {s}")

        # ── 5. Stage 4: Extract reference + generate improved waypoints ──
        print(f"\n[5] Stage 4 — Generating improved waypoints ({best_slug})")

        # 5a. Extract engage/lift joints from trace
        screw_ref = extract_screw_reference(data)
        if screw_ref is None:
            print("    ERROR: Could not extract screw reference from trace — aborting.")
        else:
            print(f"    Reference engage joints: {screw_ref['engage_joints']}")
            print(f"    Reference lift   joints: {screw_ref['lift_joints']}")

            # 5b. Ask 8B for improved parameters
            waypoint_prompt = fill_waypoint_prompt(
                best["cosmos_response"], eval_data, screw_ref
            )

            est_tokens = int(len(waypoint_prompt) / 2.5)
            max_tokens = context_limit - est_tokens - 200
            if max_tokens < 1024:
                print(f"    Waypoint prompt too large (~{est_tokens} tokens) — skipping.")
            else:
                t0 = time.time()
                try:
                    wp_response = await call_cosmos(
                        client, waypoint_prompt, COSMOS_8B_URL, COSMOS_8B_MODEL,
                        max_tokens=min(max_tokens, 4096)
                    )
                    elapsed = time.time() - t0
                except Exception as e:
                    elapsed = time.time() - t0
                    print(f"    FAILED ({elapsed:.1f}s): {e}")
                    wp_response = None

                if wp_response:
                    wp_data = parse_json_from_response(wp_response)

                    # 5c. Extract model parameters (fall back to defaults)
                    model_params = wp_data.get("screwing_parameters", {})
                    model_engage = wp_data.get("engage_joints", screw_ref["engage_joints"])
                    model_lift   = wp_data.get("lift_joints", screw_ref["lift_joints"])

                    print(f"\n    Model parameters ({elapsed:.1f}s):")
                    for k, v in model_params.items():
                        print(f"      {k}: {v}")
                    if model_engage != screw_ref["engage_joints"]:
                        print(f"    Model adjusted engage joints: {model_engage}")
                    if model_lift != screw_ref["lift_joints"]:
                        print(f"    Model adjusted lift joints:   {model_lift}")

                    # 5d. Generate full waypoint sequence from parameters
                    screw_waypoints = generate_waypoints_from_params(
                        model_params, model_engage, model_lift
                    )
                    full_waypoints = PICK_WAYPOINTS + screw_waypoints

                    wp_out = WAYPOINTS_OUT / f"waypoints_{best_slug}_{trace_id}_{ts}.json"
                    wp_full = {
                        "model":         COSMOS_8B_MODEL,
                        "source_slug":   best_slug,
                        "trace_id":      trace_id,
                        "trace_file":    str(trace_path),
                        "cosmos_file":   str(best["cosmos_file"]),
                        "eval_file":     str(best["eval_file"]),
                        "eval_score":    best_score,
                        "timestamp":     ts,
                        "elapsed_sec":   round(elapsed, 2),
                        "response":      wp_response,
                        "screwing_parameters": model_params,
                        "reference_engage_joints": screw_ref["engage_joints"],
                        "reference_lift_joints":   screw_ref["lift_joints"],
                        "model_engage_joints":     model_engage,
                        "model_lift_joints":       model_lift,
                        "recommended_waypoints":   full_waypoints,
                        "pick_waypoints_count":    len(PICK_WAYPOINTS),
                        "generated_waypoints_count": len(screw_waypoints),
                    }
                    if "task_summary" in wp_data:
                        wp_full["task_summary"] = wp_data["task_summary"]
                    if "improvements_addressed" in wp_data:
                        wp_full["improvements_addressed"] = wp_data["improvements_addressed"]

                    wp_out.write_text(json.dumps(wp_full, indent=2))

                    print(f"\n    {len(PICK_WAYPOINTS)} pick + {len(screw_waypoints)} screwing = {len(full_waypoints)} total waypoints")
                    print(f"    Waypoints file: {wp_out}")

                    if "task_summary" in wp_data:
                        print(f"\n    Task summary: {wp_data['task_summary']}")
                    if "improvements_addressed" in wp_data:
                        print(f"\n    Improvements addressed:")
                        for imp in wp_data["improvements_addressed"]:
                            print(f"      - {imp}")

        # ── 6. Final summary ───────────────────────────────────────────────
        print(f"\n{'─'*66}")
        print(f"  Pipeline complete")
        print(f"  Trace:         {trace_path.name}  (id={trace_id})")
        print(f"  Analysis:      outputs/cosmos/2b/  ({len(analysis_outputs)} files)  [{COSMOS_2B_MODEL}]")
        print(f"  Evaluations:   outputs/eval/8b/    ({len(eval_outputs)} files)  [{COSMOS_8B_MODEL}]")
        print(f"  Best slug:     {best_slug}  (score={score_str})")
        if wp_response:
            print(f"  Waypoints:     {wp_out.name}")
            print(f"\n  To execute waypoints on robot:")
            print(f"    python3 tests/execute_cosmos_waypoints.py {wp_out}")
            print(f"    python3 tests/execute_cosmos_waypoints.py {wp_out} --dry-run")
        print(f"{'─'*66}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Mixed-model pipeline: Cosmos 2B analysis → Cosmos 8B eval + waypoints"
    )
    parser.add_argument(
        "--trace",
        default=None,
        help="Path to trace file (default: screw_continuous_20260308_115305.json or latest)",
    )
    parser.add_argument("--cosmos-2b-url", default=COSMOS_2B_URL, help=f"2B vLLM URL (default: {COSMOS_2B_URL})")
    parser.add_argument("--cosmos-8b-url", default=COSMOS_8B_URL, help=f"8B vLLM URL (default: {COSMOS_8B_URL})")
    args = parser.parse_args()

    trace = Path(args.trace) if args.trace else find_default_trace()
    asyncio.run(main(trace, args.cosmos_2b_url, args.cosmos_8b_url))
