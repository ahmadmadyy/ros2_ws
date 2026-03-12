#!/usr/bin/env python3
"""
Run all 4 Llama evaluation prompts against the latest Cosmos outputs + screw trace.

For each prompt pair (cosmos_X ↔ eval_X), this script:
1. Finds the latest cosmos output for that prompt slug
2. Fills the matching eval prompt with the cosmos reasoning + trajectory
3. Calls Llama via Ollama
4. Saves the scored JSON to outputs/eval/

Outputs are saved as:
    eval_<prompt_slug>_<trace_id>_<timestamp>.json

Usage
-----
    python3 tests/run_eval_4prompts.py
    python3 tests/run_eval_4prompts.py --trace outputs/screw_trace/screw_continuous_20260310_234912.json
    python3 tests/run_eval_4prompts.py --llama-model llama3:latest
    python3 tests/run_eval_4prompts.py --cosmos-ts 20260311_121956   # pick a specific cosmos batch
"""

import argparse
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WS = Path(__file__).parent.parent
PROMPTS_DIR     = WS / "prompts"
COSMOS_OUT      = WS / "outputs" / "cosmos"
EVAL_OUT        = WS / "outputs" / "eval"
SCREW_TRACE_DIR = WS / "outputs" / "screw_trace"

OLLAMA_URL  = "http://localhost:11434"
LLAMA_MODEL = "llama3:latest"
MAX_SAMPLES = 80

# The 4 prompt pairs: (cosmos_prompt, eval_prompt, slug)
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
    screw_step_rad=-0.785,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_latest_trace() -> Path:
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


def fill_eval_prompt(template: str, cosmos_reasoning: str, trajectory_json: str) -> str:
    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_reasoning_text"] = cosmos_reasoning
    ctx["joint_trajectory_json"] = trajectory_json
    result = template
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    return result


def find_latest_cosmos_output(slug: str, cosmos_ts: str | None = None) -> Path | None:
    """Find the latest cosmos .txt output for a given prompt slug."""
    pattern = f"cosmos_{slug}_*.txt"
    candidates = sorted(COSMOS_OUT.glob(pattern), reverse=True)
    if cosmos_ts:
        # Filter to specific timestamp batch
        candidates = [c for c in candidates if cosmos_ts in c.name]
    if not candidates:
        return None
    return candidates[0]


def parse_json_from_response(text: str) -> dict:
    match = re.search(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return {"parse_error": "Could not extract JSON", "raw_response": text[:2000]}


async def call_llama(
    client: httpx.AsyncClient,
    prompt: str,
    model: str,
    ollama_url: str,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 4096},
    }
    resp = await client.post(f"{ollama_url}/api/chat", json=payload)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(
    trace_path: Path,
    llama_model: str,
    ollama_url: str,
    cosmos_ts: str | None,
):
    EVAL_OUT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── 1. Load trace ────────────────────────────────────────────────────────
    print(f"\n[1] Loading trace: {trace_path.name}")
    with open(trace_path) as f:
        data = json.load(f)
    trace_id = data.get("trace_id", "unknown")
    print(f"    trace_id={trace_id}  snapshots={data['num_snapshots']}  "
          f"duration={data['duration_sec']:.1f}s")

    trajectory_json = build_trajectory_json(data)
    print(f"    Sampled to {MAX_SAMPLES} waypoints")

    # ── 2. Check Ollama ──────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            r = await client.get(f"{ollama_url}/api/tags", timeout=5.0)
            models = [m["name"] for m in r.json().get("models", [])]
            print(f"\n[2] Ollama online — models: {models}")
        except Exception as e:
            print(f"\n[2] ERROR: Ollama not reachable at {ollama_url} — {e}")
            return

        # ── 3. Run all 4 eval prompts ────────────────────────────────────────
        print(f"\n[3] Running {len(PROMPT_PAIRS)} evaluations with {llama_model}\n")
        print(f"    {'Slug':<25} {'Cosmos file':<50} {'Time':>6}  {'Score':>6}  Output")
        print(f"    {'─'*25} {'─'*50} {'─'*6}  {'─'*6}  {'─'*40}")

        results = []
        for cosmos_prompt_file, eval_prompt_file, slug in PROMPT_PAIRS:
            # Find cosmos output
            cosmos_file = find_latest_cosmos_output(slug, cosmos_ts)
            if cosmos_file is None:
                print(f"    {slug:<25} {'NO COSMOS OUTPUT FOUND':<50} {'—':>6}  {'—':>6}  SKIPPED")
                continue

            cosmos_reasoning = cosmos_file.read_text()
            eval_template = (PROMPTS_DIR / eval_prompt_file).read_text()
            filled = fill_eval_prompt(eval_template, cosmos_reasoning, trajectory_json)

            t0 = time.time()
            try:
                response = await call_llama(client, filled, llama_model, ollama_url)
                elapsed = time.time() - t0
            except Exception as e:
                elapsed = time.time() - t0
                print(f"    {slug:<25} {cosmos_file.name:<50} {elapsed:>5.1f}s  {'ERR':>6}  FAILED: {e}")
                continue

            eval_data = parse_json_from_response(response)
            eval_data["_meta"] = {
                "eval_prompt":    eval_prompt_file,
                "cosmos_prompt":  cosmos_prompt_file,
                "slug":           slug,
                "trace_id":       trace_id,
                "trace_file":     str(trace_path),
                "cosmos_file":    str(cosmos_file),
                "llama_model":    llama_model,
                "timestamp":      ts,
                "llama_elapsed":  round(elapsed, 2),
            }

            out_file = EVAL_OUT / f"eval_{slug}_{trace_id}_{ts}.json"
            out_file.write_text(json.dumps(eval_data, indent=2))

            overall = eval_data.get("overall_score", "?")
            print(f"    {slug:<25} {cosmos_file.name:<50} {elapsed:>5.1f}s  {str(overall):>6}  {out_file.name}")
            results.append((slug, overall, eval_data.get("critical_failures", [])))

    # ── 4. Summary ───────────────────────────────────────────────────────────
    print(f"\n[4] Summary")
    print(f"    {'Slug':<25} {'Score':>6}  Critical Failures")
    print(f"    {'─'*25} {'─'*6}  {'─'*40}")
    for slug, score, failures in results:
        fail_str = ", ".join(failures) if failures else "none"
        print(f"    {slug:<25} {str(score):>6}  {fail_str}")
    print(f"\n    Results saved to: outputs/eval/eval_*_{trace_id}_{ts}.json\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Llama eval on all 4 Cosmos outputs")
    parser.add_argument("--trace",       default=None,        help="Path to trace file (default: latest)")
    parser.add_argument("--ollama-url",  default=OLLAMA_URL,  help="Ollama base URL")
    parser.add_argument("--llama-model", default=LLAMA_MODEL, help="Llama model name")
    parser.add_argument("--cosmos-ts",   default=None,        help="Filter cosmos outputs by timestamp (e.g. 20260311_121956)")
    args = parser.parse_args()

    trace = Path(args.trace) if args.trace else find_latest_trace()
    asyncio.run(main(trace, args.llama_model, args.ollama_url, args.cosmos_ts))
