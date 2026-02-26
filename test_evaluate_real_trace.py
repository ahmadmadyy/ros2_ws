#!/usr/bin/env python3
"""
Evaluate a real recorded pick trace through the Cosmos → Llama pipeline.

Loads a pick_trace_*.json file from disk, reconstructs the ExecutionTrace,
calls Cosmos Reason2 for explainability (if available), then evaluates
the reasoning + trajectory with a local Llama model via Ollama.

Usage:
    python3 test_evaluate_real_trace.py
    python3 test_evaluate_real_trace.py pick_trace_20260221_144709.json
    python3 test_evaluate_real_trace.py pick_trace_20260221_144709.json --model llama3:latest
    python3 test_evaluate_real_trace.py --cosmos-url http://localhost:8000
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src" / "robot_agent"))

from robot_agent.cosmos.cosmos_client import CosmosClient
from robot_agent.cosmos.explainability import ExplainabilityEngine
from robot_agent.cosmos.ollama_client import OllamaClient
from robot_agent.cosmos.evaluator import EvaluationEngine
from robot_agent.recorder.execution_trace import ExecutionTrace, JointSnapshot


# ---------------------------------------------------------------------------
# Trace loader: converts pick_trace_*.json → ExecutionTrace
# ---------------------------------------------------------------------------

def load_trace_from_json(path: Path) -> ExecutionTrace:
    """
    Convert a pick_trace_*.json file (saved by the test script) back to an
    ExecutionTrace.  The JSON format is:
        { "trace_id": ..., "label": ..., "duration_sec": ...,
          "trajectory": [ {"t": float, "joints": {name: value}}, ... ] }
    """
    with open(path) as f:
        data = json.load(f)

    trajectory = data.get("trajectory", [])
    if not trajectory:
        raise ValueError(f"No trajectory data found in {path}")

    # Determine joint name order from the first row
    first_joints = trajectory[0]["joints"]
    joint_names = list(first_joints.keys())

    base_time = 1_700_000_000.0  # arbitrary epoch; only differences matter
    snapshots = []
    for row in trajectory:
        t = float(row["t"])
        positions = [float(row["joints"].get(j, 0.0)) for j in joint_names]
        snapshots.append(JointSnapshot(
            timestamp=base_time + t,
            joint_names=joint_names,
            positions=positions,
            velocities=[0.0] * len(joint_names),
        ))

    start_time = base_time + float(trajectory[0]["t"])
    end_time   = base_time + float(trajectory[-1]["t"])

    return ExecutionTrace(
        trace_id=data.get("trace_id", path.stem),
        label=data.get("label", "pick"),
        snapshots=snapshots,
        start_time=start_time,
        end_time=end_time,
    )


def find_latest_trace(root: Path) -> Path:
    candidates = sorted(root.glob("pick_trace_*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No pick_trace_*.json files found in {root}")
    return candidates[0]


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _bar(score, out_of=5, width=20) -> str:
    if not isinstance(score, (int, float)):
        return "?" * width
    filled = round(score / out_of * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {score}/{out_of}"


def print_results(result: dict):
    print("\n" + "=" * 68)
    print("  EVALUATION RESULTS")
    print("=" * 68)

    dimensions = [
        ("goal_achievement",  "Goal Achievement  (25%)"),
        ("consistency",       "Consistency       (20%)"),
        ("plan_coherence",    "Plan Coherence    (15%)"),
        ("smoothness",        "Smoothness        (15%)"),
        ("safety_awareness",  "Safety Awareness  (10%)"),
        ("efficiency",        "Efficiency        (10%)"),
        ("spatial_awareness", "Spatial Awareness  (5%)"),
    ]

    for key, label in dimensions:
        dim = result.get(key)
        if not isinstance(dim, dict):
            continue
        score = dim.get("score", 0)
        just  = dim.get("justification", "")
        print(f"\n  {label}")
        print(f"  {_bar(score)}")
        if just:
            words = just.split()
            line, lines = "", []
            for w in words:
                if len(line) + len(w) + 1 > 62:
                    lines.append(line)
                    line = w
                else:
                    line = (line + " " + w).strip()
            if line:
                lines.append(line)
            for l in lines:
                print(f"    {l}")

    overall = result.get("overall_score", 0.0)
    print(f"\n{'─' * 68}")
    print(f"  OVERALL SCORE : {overall:.3f} / 5.000")
    print(f"{'─' * 68}")

    failures = result.get("critical_failures") or []
    if failures and failures != ["None"]:
        print("\n  Critical Failures:")
        for f in failures:
            print(f"    ✗ {f}")

    suggestions = result.get("suggestions") or []
    if suggestions:
        print("\n  Suggestions:")
        for s in suggestions:
            print(f"    → {s}")

    if result.get("parse_error"):
        print(f"\n  [PARSE ERROR] {result['parse_error']}")
        print(f"  Raw:\n{result.get('raw_response', '')[:400]}")

    print("=" * 68 + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(trace_path: Path, cosmos_url: str, ollama_url: str, model: str):
    print(f"\nTrace file  : {trace_path}")
    print(f"Cosmos URL  : {cosmos_url}")
    print(f"Ollama URL  : {ollama_url}")
    print(f"Llama model : {model}")

    # ── 1. Load trace ──────────────────────────────────────────────────────
    print("\n[1/4] Loading trace from disk...")
    trace = load_trace_from_json(trace_path)
    print(f"  trace_id   : {trace.trace_id}")
    print(f"  label      : {trace.label}")
    print(f"  snapshots  : {len(trace.snapshots)}")
    print(f"  duration   : {trace.duration_sec:.2f} s")
    print(f"  joints     : {trace.snapshots[0].joint_names if trace.snapshots else '?'}")

    # ── 2. Cosmos: explain the trace ───────────────────────────────────────
    print("\n[2/4] Cosmos explainability...")
    cosmos = CosmosClient(base_url=cosmos_url)
    cosmos_available = await cosmos.is_available()

    if cosmos_available:
        print("  Cosmos is available — generating explanation...")
        explainer = ExplainabilityEngine(cosmos)
        cosmos_reasoning = await explainer.explain_trace(
            trace,
            task_description="Pick a screwdriver from the table using UR5e + Robotiq 2F-85 gripper",
        )
        print(f"  Explanation ({len(cosmos_reasoning)} chars):")
        print("  " + cosmos_reasoning[:400].replace("\n", "\n  ") + " ...")
    else:
        print(f"  Cosmos NOT reachable at {cosmos_url} — generating trajectory summary instead.")
        # Build a factual summary from the trajectory data so Ollama still has context
        summary = trace.to_summary_text(max_rows=20)
        cosmos_reasoning = (
            "Cosmos Reason2 was not available. The following is the raw trajectory summary "
            "derived directly from the joint-state recording:\n\n" + summary
        )
        print("  Using trajectory summary as fallback reasoning.")

    # ── 3. Ollama: evaluate ────────────────────────────────────────────────
    print("\n[3/4] Ollama evaluation...")
    ollama = OllamaClient(base_url=ollama_url, model=model)
    if not await ollama.is_available():
        print(f"  ERROR: Ollama not reachable at {ollama_url}")
        return

    models = await ollama.list_models()
    print(f"  Ollama OK — local models: {models}")

    evaluator = EvaluationEngine(ollama)
    print("  Sending to Llama (this may take 30–90 s)...")

    result = await evaluator.evaluate(
        trace=trace,
        cosmos_reasoning_text=cosmos_reasoning,
        task_description="Pick a screwdriver at x=0.45, y=0.00, z=0.09 (centre of mass) "
                         "from the planning scene and lift it 0.18 m upward",
        object_name="screwdriver",
        pick_position="[0.45, 0.00, 0.09]",
        place_position="N/A (pick only)",
        robot_description="UR5e 6-DOF manipulator with Robotiq 2F-85 parallel gripper",
    )

    # ── 4. Print results ───────────────────────────────────────────────────
    print("\n[4/4] Results:")
    print_results(result)

    out_path = trace_path.parent / f"eval_{trace.trace_id}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"Full JSON saved → {out_path}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate a real pick trace through Cosmos → Llama pipeline"
    )
    parser.add_argument(
        "trace_file", nargs="?", default=None,
        help="Path to pick_trace_*.json (default: most recent in current dir)",
    )
    parser.add_argument("--cosmos-url", default="http://localhost:8000")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--model",      default="llama3:latest")
    args = parser.parse_args()

    root = Path(__file__).parent
    trace_path = Path(args.trace_file) if args.trace_file else find_latest_trace(root)

    asyncio.run(run(
        trace_path=trace_path,
        cosmos_url=args.cosmos_url,
        ollama_url=args.ollama_url,
        model=args.model,
    ))


if __name__ == "__main__":
    main()
