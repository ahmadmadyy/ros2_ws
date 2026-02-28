#!/usr/bin/env python3
"""
Cosmos + Llama analysis pipeline — uses the live in-memory trace from the agent.

After a pick has been executed via the agent, this script:
  1. Finds the trace (by trace_id, or picks the latest from the agent)
  2. Sends it to Cosmos Reason2-8B  (Prompt 1 — trajectory analysis)
  3. Sends Cosmos output to Llama   (Prompt 2 — judge / score)
  4. Prints both outputs in full

Usage
-----
  # Auto-pick the latest trace in the agent:
  python3 test_cosmos_pipeline.py

  # Use a specific trace_id returned by the pick skill:
  python3 test_cosmos_pipeline.py --trace-id <trace_id>

  # Skip Cosmos (test Llama judge only, supply reasoning manually):
  python3 test_cosmos_pipeline.py --trace-id <id> \
      --cosmos-analysis "The robot approached from above and grasped successfully."
"""

import argparse
import asyncio
import json
import sys
import time

import httpx

AGENT_URL  = "http://localhost:8080"
COSMOS_URL = "http://localhost:8000"

# Default task parameters — match the pick skill call
PICK_PARAMS = {
    "instruction":       "Pick the screwdriver from the table and lift it clear",
    "object_name":       "screwdriver",
    "pick_x":            0.45,
    "pick_y":            0.00,
    "pick_z":            0.09,
    "object_dims":       "cylinder: diameter 24 mm, height 180 mm (upright)",
    "grasp_z":           0.26,
    "approach_height":   0.18,
    "retreat_height":    0.18,
    "grasp_gripper_rad": 0.57,
    "place_description": "N/A — pick only",
    "scene_context": (
        "Flat table. Screwdriver upright in MoveIt planning scene at x=0.45, y=0.00. "
        "No other obstacles. Robot base at world origin."
    ),
}


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _bar(score, out_of=5, width=20) -> str:
    if not isinstance(score, (int, float)):
        return "?" * width
    filled = round(float(score) / out_of * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {score}/{out_of}"


def print_cosmos(text: str):
    print()
    print("─" * 72)
    print("  COSMOS REASON2  —  Trajectory Analysis  (Prompt 1)")
    print("─" * 72)
    print(text)
    print("─" * 72)


def print_judge(data: dict):
    print()
    print("=" * 72)
    print("  LLAMA JUDGE  —  Cosmos Analysis Scores  (Prompt 2)")
    print("=" * 72)

    dims = [
        ("goal_achievement",    "Goal Achievement    (25%)"),
        ("consistency",         "Consistency         (20%)"),
        ("plan_coherence",      "Plan Coherence      (15%)"),
        ("corrective_feedback", "Corrective Feedback (15%)"),
        ("safety_awareness",    "Safety Awareness    (10%)"),
        ("anomaly_detection",   "Anomaly Detection   (10%)"),
        ("spatial_awareness",   "Spatial Awareness    (5%)"),
    ]

    for key, label in dims:
        dim = data.get(key) or {}
        score = dim.get("score", "?")
        just  = dim.get("justification", "")
        print(f"\n  {label}")
        print(f"  {_bar(score)}")
        if just:
            words, line, lines = just.split(), "", []
            for w in words:
                if len(line) + len(w) + 1 > 66:
                    lines.append(line); line = w
                else:
                    line = (line + " " + w).strip()
            if line:
                lines.append(line)
            for l in lines:
                print(f"    {l}")

    overall = data.get("overall_score", 0.0)
    outcome = data.get("task_outcome", "unknown").upper()
    print(f"\n{'─' * 72}")
    print(f"  OVERALL SCORE  : {overall:.3f} / 5.000")
    print(f"  TASK OUTCOME   : {outcome}")
    print(f"{'─' * 72}")

    failures = [f for f in (data.get("critical_failures") or []) if f and f != "None"]
    if failures:
        print("\n  Critical Failures:")
        for f in failures:
            print(f"    ✗ {f}")

    suggestions = data.get("suggestions") or []
    if suggestions:
        print("\n  Suggestions:")
        for s in suggestions:
            print(f"    → {s}")

    if data.get("parse_error"):
        print(f"\n  [PARSE ERROR] {data['parse_error']}")

    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(trace_id: str | None, cosmos_analysis: str | None):
    async with httpx.AsyncClient(timeout=600.0) as client:

        # ── 0. Agent health check ──────────────────────────────────────────
        print("\n[0] Checking agent …")
        try:
            h = (await client.get(f"{AGENT_URL}/api/v1/health")).json()
        except Exception as e:
            print(f"  ERROR: Agent not reachable — {e}")
            print("  Run:  ros2 run robot_agent robot_agent")
            return
        print(f"  Status : {h['status']}")
        print(f"  ROS    : {'connected' if h['ros_connected'] else 'NOT connected (trace from memory still works)'}")
        print(f"  Ollama : {'OK' if h['ollama_available'] else 'NOT available'}")

        # ── 1. Cosmos model check ──────────────────────────────────────────
        print("\n[1] Checking Cosmos …")
        try:
            m = (await client.get(f"{AGENT_URL}/api/v1/model")).json()
            print(f"  Configured : {m['configured_model']}")
            print(f"  Serving    : {m.get('served_model', 'unknown')}")
            if not m.get("available"):
                print("  WARNING: Cosmos not available — start vLLM or check server")
        except Exception as e:
            print(f"  Could not reach model endpoint: {e}")

        # ── 2. Resolve trace ───────────────────────────────────────────────
        print("\n[2] Resolving trace …")
        if trace_id:
            print(f"  Using supplied trace_id: {trace_id}")
        else:
            # Pick the latest trace from the agent's in-memory store
            try:
                traces = (await client.get(f"{AGENT_URL}/api/v1/traces")).json()
            except Exception as e:
                print(f"  ERROR listing traces: {e}")
                return
            if not traces:
                print("  No traces found in agent. Run a pick first:")
                print('  curl -s -X POST http://localhost:8080/api/v1/skill/pick \\')
                print('    -H "Content-Type: application/json" \\')
                print('    -d \'{"params": {"pose": {"x":0.45,"y":0.00,"z":0.26,')
                print('                            "qx":1.0,"qy":0.0,"qz":0.0,"qw":0.0},')
                print('                "approach_height":0.18, "retreat_height":0.18,')
                print('                "grasp_gripper_position":0.57, "object_id":"screwdriver"}}\'')
                return
            # Most recent trace is last in the list
            latest = traces[-1]
            trace_id = latest["trace_id"]
            print(f"  Latest trace in agent:")
            print(f"    trace_id  : {trace_id}")
            print(f"    label     : {latest['label']}")
            print(f"    snapshots : {latest['num_snapshots']}")
            print(f"    duration  : {latest['duration_sec']} s")

        # ── 3. Analyze ─────────────────────────────────────────────────────
        payload = dict(PICK_PARAMS)
        if cosmos_analysis:
            payload["cosmos_analysis"] = cosmos_analysis
            print(f"\n[3] POST /api/v1/analyze/{trace_id}  (Llama judge only — Cosmos skipped)")
        else:
            print(f"\n[3] POST /api/v1/analyze/{trace_id}")
            print("  Step 3a ▶ Cosmos Reason2  (Prompt 1 — trajectory analysis) …")

        t0 = time.monotonic()
        try:
            resp = await client.post(
                f"{AGENT_URL}/api/v1/analyze/{trace_id}",
                json=payload,
            )
        except httpx.ReadTimeout:
            print("  ERROR: Request timed out. Cosmos may still be loading.")
            return

        elapsed = time.monotonic() - t0

        if resp.status_code != 200:
            print(f"  ERROR {resp.status_code}: {resp.text[:400]}")
            return

        data = resp.json()
        print(f"  Done in {elapsed:.1f} s")

        # ── 4. Print results ────────────────────────────────────────────────
        print_cosmos(data.get("cosmos_analysis", "(no output)"))

        print("\n  Step 3b ▶ Llama judge  (Prompt 2 — scoring Cosmos output) …")
        print_judge(data)

        # ── 5. Save ────────────────────────────────────────────────────────
        from pathlib import Path
        out = Path(__file__).parent / f"analysis_{trace_id}.json"
        out.write_text(json.dumps(data, indent=2))
        print(f"\nFull JSON saved → {out}")


def main():
    p = argparse.ArgumentParser(description="Cosmos + Llama analysis of a pick trace")
    p.add_argument("--trace-id", default=None,
                   help="trace_id from the pick skill response (default: latest in agent)")
    p.add_argument("--cosmos-analysis", default=None,
                   help="Pre-supply Cosmos analysis text (skips Cosmos call)")
    args = p.parse_args()
    asyncio.run(run(args.trace_id, args.cosmos_analysis))


if __name__ == "__main__":
    main()
