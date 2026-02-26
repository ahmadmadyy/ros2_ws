#!/usr/bin/env python3
"""
Standalone test for the Cosmos → Llama evaluation pipeline.

Runs without ROS2. Requires Ollama to be running locally with a Llama model pulled.

Usage:
    python3 test_evaluation.py
    python3 test_evaluation.py --ollama-url http://localhost:11434 --model llama3.2
"""

import asyncio
import json
import sys
import argparse
from pathlib import Path

# Make the robot_agent package importable without a ROS2 install
sys.path.insert(0, str(Path(__file__).parent / "src" / "robot_agent"))

from robot_agent.cosmos.ollama_client import OllamaClient
from robot_agent.cosmos.evaluator import EvaluationEngine
from robot_agent.recorder.execution_trace import ExecutionTrace, JointSnapshot

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_COSMOS_REASONING = """
The robot is performing a pick-and-place task involving a red cube located at
approximately (0.40, 0.10, 0.05) in the robot's base frame.

Step 1 – Pre-grasp approach:
The arm moves from its home configuration to a position directly above the
object. The shoulder_lift joint rotates to roughly -1.2 rad and the elbow
joint to ~1.4 rad, bringing the wrist above the target at a safe clearance
of ~15 cm. Collision avoidance with the table surface is maintained.

Step 2 – Descent:
wrist_1 and wrist_2 are adjusted so the gripper faces downward (tool-frame
Z pointing toward the table). The arm descends by increasing shoulder_lift
by ~0.3 rad while keeping the wrist orientation fixed.

Step 3 – Grasp:
The Robotiq 2F-85 gripper closes from 0.0 (open) to 0.79 rad (fully closed)
to secure the cube. Effort is kept below the maximum to avoid crushing.

Step 4 – Lift:
The arm lifts ~10 cm by reversing the descent motion, keeping the object
stable and within joint limits.

Step 5 – Transport:
shoulder_pan rotates approximately -0.6 rad to sweep the arm toward the
goal shelf position at (0.20, -0.30, 0.30). Smooth interpolation is used
throughout to avoid jerky motion.

Step 6 – Placement:
The arm descends to the shelf height, releases the gripper, and returns to
a neutral configuration.

The plan is logically ordered, spatially aware, and accounts for the table
surface during approach and placement. No joint limit violations are expected.
""".strip()

# Simulated UR5e joint trajectory for a pick-and-place task
# Columns: shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3, gripper
_JOINT_NAMES = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
    "rq_robotiq_85_left_knuckle_joint",
]

_WAYPOINTS = [
    # t=0.0  home
    (0.00,  [0.000, -1.571, 0.000, -1.571, 0.000, 0.000, 0.00]),
    # t=1.0  move above object
    (1.00,  [0.150, -1.200, 1.400, -1.750, -1.571, 0.000, 0.00]),
    # t=2.0  descend to grasp height
    (2.00,  [0.150, -0.900, 1.400, -2.050, -1.571, 0.000, 0.00]),
    # t=2.5  close gripper
    (2.50,  [0.150, -0.900, 1.400, -2.050, -1.571, 0.000, 0.79]),
    # t=3.0  lift
    (3.00,  [0.150, -1.200, 1.400, -1.750, -1.571, 0.000, 0.79]),
    # t=4.0  sweep to goal
    (4.00,  [-0.450, -1.100, 1.300, -1.800, -1.571, 0.000, 0.79]),
    # t=5.0  descend to shelf
    (5.00,  [-0.450, -0.850, 1.300, -2.000, -1.571, 0.000, 0.79]),
    # t=5.5  open gripper (release)
    (5.50,  [-0.450, -0.850, 1.300, -2.000, -1.571, 0.000, 0.00]),
    # t=6.5  return to home
    (6.50,  [0.000, -1.571, 0.000, -1.571, 0.000, 0.000, 0.00]),
]


def _build_mock_trace() -> ExecutionTrace:
    base_time = 1_700_000_000.0
    snapshots = []
    for t_offset, positions in _WAYPOINTS:
        snapshots.append(JointSnapshot(
            timestamp=base_time + t_offset,
            joint_names=_JOINT_NAMES,
            positions=positions,
            velocities=[0.0] * len(_JOINT_NAMES),
        ))
    return ExecutionTrace(
        trace_id="test-001",
        label="pick_red_cube",
        snapshots=snapshots,
        start_time=base_time,
        end_time=base_time + _WAYPOINTS[-1][0],
    )


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _score_bar(score: int, out_of: int = 5) -> str:
    filled = round(score / out_of * 20)
    return "[" + "█" * filled + "░" * (20 - filled) + f"] {score}/{out_of}"


def _print_results(result: dict):
    print("\n" + "=" * 65)
    print("  LLAMA EVALUATION RESULTS")
    print("=" * 65)

    dimensions = [
        ("goal_achievement",  "Goal Achievement "),
        ("consistency",       "Consistency      "),
        ("plan_coherence",    "Plan Coherence   "),
        ("smoothness",        "Smoothness       "),
        ("safety_awareness",  "Safety Awareness "),
        ("efficiency",        "Efficiency       "),
        ("spatial_awareness", "Spatial Awareness"),
    ]

    for key, label in dimensions:
        dim = result.get(key, {})
        if isinstance(dim, dict):
            score = dim.get("score", 0)
            just  = dim.get("justification", "")
            print(f"\n  {label}  {_score_bar(score)}")
            if just:
                # Wrap justification at 60 chars
                words = just.split()
                line, lines = "", []
                for w in words:
                    if len(line) + len(w) + 1 > 58:
                        lines.append(line)
                        line = w
                    else:
                        line = (line + " " + w).strip()
                if line:
                    lines.append(line)
                for l in lines:
                    print(f"    {l}")

    overall = result.get("overall_score", 0.0)
    print(f"\n{'─' * 65}")
    print(f"  OVERALL SCORE : {overall:.3f} / 5.000")
    print(f"{'─' * 65}")

    failures = result.get("critical_failures", [])
    if failures:
        print("\n  Critical Failures:")
        for f in failures:
            print(f"    ✗ {f}")

    suggestions = result.get("suggestions", [])
    if suggestions:
        print("\n  Suggestions:")
        for s in suggestions:
            print(f"    → {s}")

    if result.get("parse_error"):
        print(f"\n  [PARSE ERROR] {result['parse_error']}")
        print(f"  Raw response:\n{result.get('raw_response', '')}")

    print("=" * 65 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(ollama_url: str, model: str):
    print(f"\nOllama URL : {ollama_url}")
    print(f"Model      : {model}")

    ollama = OllamaClient(base_url=ollama_url, model=model)

    # 1. Connectivity check
    print("\n[1/4] Checking Ollama availability...")
    if not await ollama.is_available():
        print(f"  ERROR: Cannot reach Ollama at {ollama_url}")
        print("  Make sure Ollama is running:  ollama serve")
        return

    available_models = await ollama.list_models()
    print(f"  OK — local models: {available_models or '(none listed)'}")
    if model not in " ".join(available_models):
        print(f"  WARNING: '{model}' not found in local model list.")
        print(f"  Pull it with:  ollama pull {model}")

    # 2. Build mock inputs
    print("\n[2/4] Building mock trace and Cosmos reasoning...")
    trace = _build_mock_trace()
    trajectory_preview = trace.to_trajectory_json(max_rows=5)
    print(f"  Trace   : {trace.trace_id}  ({len(trace.snapshots)} snapshots, "
          f"{trace.duration_sec:.1f}s)")
    print(f"  Traj    : {len(trace.to_trajectory_json())} rows (preview: first 5 below)")
    print("  " + json.dumps(trajectory_preview, indent=4).replace("\n", "\n  "))
    print(f"\n  Cosmos reasoning ({len(MOCK_COSMOS_REASONING)} chars):")
    print("  " + MOCK_COSMOS_REASONING[:300].replace("\n", "\n  ") + " ...")

    # 3. Run evaluation
    engine = EvaluationEngine(ollama)
    print("\n[3/4] Sending to Llama for evaluation (this may take 30–90 s)...")
    result = await engine.evaluate(
        trace=trace,
        cosmos_reasoning_text=MOCK_COSMOS_REASONING,
        task_description="Pick red cube from table and place it on the shelf",
        object_name="red_cube",
        pick_position="[0.40, 0.10, 0.05]",
        place_position="[0.20, -0.30, 0.30]",
        robot_description="UR5e 6-DOF manipulator with Robotiq 2F-85 parallel gripper",
    )

    # 4. Show results
    print("\n[4/4] Results:")
    _print_results(result)

    print("Raw JSON (for debugging):")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Test Cosmos → Llama evaluation pipeline")
    parser.add_argument("--ollama-url", default="http://localhost:11434",
                        help="Ollama server URL (default: http://localhost:11434)")
    parser.add_argument("--model", default="llama3.2",
                        help="Ollama model name (default: llama3.2)")
    args = parser.parse_args()

    asyncio.run(run(args.ollama_url, args.model))


if __name__ == "__main__":
    main()
