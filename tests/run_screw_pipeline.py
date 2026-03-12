#!/usr/bin/env python3
"""
Standalone Cosmos → Llama screwing-trace evaluation pipeline.

Steps
-----
1. Find the latest screw_trace/screw_continuous_*.json  (or pass one explicitly)
2. Sample the trace down to a manageable size
3. Fill prompts/cosmos_screw.txt  → call Cosmos 2B → save outputs/cosmos/
4. Fill prompts/eval_screw.txt    → call Llama       → save outputs/eval/

Usage
-----
    python3 tests/run_screw_pipeline.py
    python3 tests/run_screw_pipeline.py --trace outputs/screw_trace/screw_continuous_20260310_213930.json
    python3 tests/run_screw_pipeline.py --cosmos-url http://localhost:8000
    python3 tests/run_screw_pipeline.py --llama-model llama3:latest
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
PROMPTS_DIR   = WS / "prompts"
COSMOS_OUT    = WS / "outputs" / "cosmos"
EVAL_OUT      = WS / "outputs" / "eval"
SCREW_TRACE_DIR = WS / "outputs" / "screw_trace"

COSMOS_URL  = "http://localhost:8000"
OLLAMA_URL  = "http://localhost:11434"
COSMOS_MODEL = "nvidia/Cosmos-Reason2-2B"
LLAMA_MODEL  = "llama3:latest"

# How many snapshots to keep when sampling (avoids token overload)
MAX_SAMPLES = 80

# ---------------------------------------------------------------------------
# Task context — matches the parameters used when the trace was recorded
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


def sample_trace(data: dict, max_samples: int = MAX_SAMPLES) -> list[dict]:
    """Return evenly-spaced snapshots including first and last."""
    snaps = data["snapshots"]
    n = len(snaps)
    if n <= max_samples:
        return snaps

    indices = [round(i * (n - 1) / (max_samples - 1)) for i in range(max_samples)]
    seen, result = set(), []
    for idx in indices:
        if idx not in seen:
            seen.add(idx)
            result.append(snaps[idx])
    return result


def build_trajectory_json(data: dict) -> str:
    """Convert sampled snapshots to a compact trajectory JSON string."""
    sampled = sample_trace(data)
    t0 = data["snapshots"][0]["timestamp"]
    rows = []
    for s in sampled:
        joint_names = s["joint_names"]
        positions   = s["positions"]
        # Arm joints only (skip gripper internals, keep left_knuckle for gripper state)
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


def fill_prompt(template: str, cosmos_reasoning: str, trajectory_json: str) -> str:
    """Fill all {placeholders} in the prompt template."""
    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_reasoning_text"] = cosmos_reasoning
    ctx["joint_trajectory_json"] = trajectory_json
    result = template
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    # Leave any remaining unfilled placeholders visible but harmless
    return result


def trace_id_from_data(data: dict) -> str:
    return data.get("trace_id", "unknown")


def slug_from_path(path: Path) -> str:
    """e.g. screw_continuous_20260310_213930"""
    return path.stem


# ---------------------------------------------------------------------------
# Cosmos call
# ---------------------------------------------------------------------------

async def call_cosmos(
    client: httpx.AsyncClient,
    prompt: str,
    model: str,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": 4096,
    }
    resp = await client.post(f"{COSMOS_URL}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Llama / Ollama call
# ---------------------------------------------------------------------------

async def call_llama(
    client: httpx.AsyncClient,
    prompt: str,
    model: str,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 4096},
    }
    resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def parse_json_from_response(text: str) -> dict:
    """Extract the first JSON object/array from a model response."""
    match = re.search(r"```(?:json)?\s*(\{[\s\S]+?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON
    match = re.search(r"(\{[\s\S]+\})", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return {"parse_error": "Could not extract JSON", "raw_response": text[:2000]}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def run(trace_path: Path, cosmos_model: str, llama_model: str):
    COSMOS_OUT.mkdir(parents=True, exist_ok=True)
    EVAL_OUT.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── 1. Load trace ──────────────────────────────────────────────────────
    print(f"\n[1/4] Loading trace: {trace_path.name}")
    with open(trace_path) as f:
        data = json.load(f)
    trace_id = trace_id_from_data(data)
    print(f"      trace_id={trace_id}  snapshots={data['num_snapshots']}  "
          f"duration={data['duration_sec']:.1f}s")

    trajectory_json = build_trajectory_json(data)
    print(f"      Sampled to {MAX_SAMPLES} waypoints for prompt injection")

    # ── 2. Load prompts ────────────────────────────────────────────────────
    cosmos_template = (PROMPTS_DIR / "cosmos_screw.txt").read_text()
    eval_template   = (PROMPTS_DIR / "eval_screw.txt").read_text()

    # ── 3. Cosmos call ─────────────────────────────────────────────────────
    print(f"\n[2/4] Calling Cosmos ({cosmos_model}) ...")
    cosmos_prompt = fill_prompt(cosmos_template, cosmos_reasoning="N/A — this is the initial trajectory analysis.", trajectory_json=trajectory_json)

    async with httpx.AsyncClient(timeout=600.0) as client:

        # Check Cosmos
        try:
            r = await client.get(f"{COSMOS_URL}/v1/models", timeout=5.0)
            served = [m["id"] for m in r.json().get("data", [])]
            print(f"      vLLM online — served: {served}")
        except Exception as e:
            print(f"      ERROR: Cosmos vLLM not reachable — {e}")
            return

        t0 = time.time()
        cosmos_output = await call_cosmos(client, cosmos_prompt, cosmos_model)
        cosmos_elapsed = time.time() - t0
        print(f"      Done in {cosmos_elapsed:.1f}s  ({len(cosmos_output)} chars)")

        # Save Cosmos output
        cosmos_file = COSMOS_OUT / f"cosmos_{trace_id}_{ts}.txt"
        cosmos_file.write_text(cosmos_output)
        print(f"      Saved → {cosmos_file.relative_to(WS)}")

        # ── 4. Llama call ──────────────────────────────────────────────────
        print(f"\n[3/4] Calling Llama ({llama_model}) ...")

        # Check Ollama
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
            models = [m["name"] for m in r.json().get("models", [])]
            print(f"      Ollama online — models: {models}")
        except Exception as e:
            print(f"      ERROR: Ollama not reachable — {e}")
            return

        eval_prompt = fill_prompt(eval_template, cosmos_reasoning=cosmos_output, trajectory_json=trajectory_json)

        t0 = time.time()
        llama_output = await call_llama(client, eval_prompt, llama_model)
        llama_elapsed = time.time() - t0
        print(f"      Done in {llama_elapsed:.1f}s  ({len(llama_output)} chars)")

        # Parse + save eval output
        eval_data = parse_json_from_response(llama_output)
        eval_data["_meta"] = {
            "trace_id":       trace_id,
            "trace_file":     str(trace_path),
            "cosmos_model":   cosmos_model,
            "llama_model":    llama_model,
            "timestamp":      ts,
            "cosmos_elapsed": round(cosmos_elapsed, 2),
            "llama_elapsed":  round(llama_elapsed, 2),
            "cosmos_file":    str(cosmos_file),
        }

        eval_file = EVAL_OUT / f"eval_{trace_id}_{ts}.json"
        eval_file.write_text(json.dumps(eval_data, indent=2))
        print(f"      Saved → {eval_file.relative_to(WS)}")

    # ── 5. Summary ─────────────────────────────────────────────────────────
    print(f"\n[4/4] Summary")
    overall = eval_data.get("overall_score", "?")
    cycles  = eval_data.get("cycles_completed", "?")
    cw_deg  = eval_data.get("total_cw_rotation_deg", "?")
    failures = eval_data.get("critical_failures", [])
    print(f"      overall_score    : {overall}")
    print(f"      cycles_completed : {cycles}")
    print(f"      total_cw_deg     : {cw_deg}")
    if failures:
        print(f"      critical_failures:")
        for f in failures:
            print(f"        ✗ {f}")
    else:
        print(f"      critical_failures: none")

    print(f"\n  Cosmos output → {cosmos_file.relative_to(WS)}")
    print(f"  Eval   output → {eval_file.relative_to(WS)}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Screw trace → Cosmos → Llama pipeline")
    parser.add_argument("--trace",       default=None,         help="Path to screw_continuous_*.json (default: latest)")
    parser.add_argument("--cosmos-url",  default=COSMOS_URL,   help="vLLM base URL")
    parser.add_argument("--cosmos-model",default=COSMOS_MODEL, help="Cosmos model ID")
    parser.add_argument("--ollama-url",  default=OLLAMA_URL,   help="Ollama base URL")
    parser.add_argument("--llama-model", default=LLAMA_MODEL,  help="Llama model name in Ollama")
    args = parser.parse_args()

    trace_path = Path(args.trace) if args.trace else find_latest_trace()
    print(f"Trace: {trace_path}")

    asyncio.run(run(
        trace_path=trace_path,
        cosmos_model=args.cosmos_model,
        llama_model=args.llama_model,
    ))


if __name__ == "__main__":
    main()
