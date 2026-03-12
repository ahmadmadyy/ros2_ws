#!/usr/bin/env python3
"""
Run all 4 Cosmos screwing prompts against the latest screw trace using Cosmos 2B.

Outputs are saved to outputs/cosmos/ with filenames that include the prompt name:
    cosmos_<prompt_slug>_<trace_id>_<timestamp>.txt
    cosmos_<prompt_slug>_<trace_id>_<timestamp>.json   (includes metadata)

Usage
-----
    python3 tests/run_cosmos_4prompts.py
    python3 tests/run_cosmos_4prompts.py --trace outputs/screw_trace/screw_continuous_20260310_234912.json
    python3 tests/run_cosmos_4prompts.py --cosmos-model nvidia/Cosmos-Reason2-8B
"""

import argparse
import asyncio
import json
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
SCREW_TRACE_DIR = WS / "outputs" / "screw_trace"

COSMOS_URL   = "http://localhost:8000"
COSMOS_MODEL = "nvidia/Cosmos-Reason2-2B"
MAX_SAMPLES  = 80

# The 4 cosmos prompts to run
COSMOS_PROMPTS = [
    "cosmos_screw.txt",
    "cosmos_fault_detection.txt",
    "cosmos_energy_dynamics.txt",
    "cosmos_process_quality.txt",
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
# Helpers (reused from run_screw_pipeline.py)
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


def fill_prompt(template: str, trajectory_json: str) -> str:
    ctx = dict(TASK_CONTEXT)
    ctx["cosmos_reasoning_text"] = "N/A — this is the initial trajectory analysis."
    ctx["joint_trajectory_json"] = trajectory_json
    result = template
    for key, val in ctx.items():
        result = result.replace("{" + key + "}", str(val))
    return result


def prompt_slug(filename: str) -> str:
    """cosmos_screw.txt -> screw, cosmos_fault_detection.txt -> fault_detection"""
    return filename.replace("cosmos_", "").replace(".txt", "")


async def call_cosmos(
    client: httpx.AsyncClient,
    prompt: str,
    model: str,
    url: str,
) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": 4096,
    }
    resp = await client.post(f"{url}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(trace_path: Path, cosmos_model: str, cosmos_url: str):
    COSMOS_OUT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── 1. Load trace ──────────────────────────────────────────────────────
    print(f"\n[1] Loading trace: {trace_path.name}")
    with open(trace_path) as f:
        data = json.load(f)
    trace_id = data.get("trace_id", "unknown")
    print(f"    trace_id={trace_id}  snapshots={data['num_snapshots']}  "
          f"duration={data['duration_sec']:.1f}s")

    trajectory_json = build_trajectory_json(data)
    print(f"    Sampled to {MAX_SAMPLES} waypoints")

    # ── 2. Check vLLM ─────────────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            r = await client.get(f"{cosmos_url}/v1/models", timeout=5.0)
            served = [m["id"] for m in r.json().get("data", [])]
            print(f"\n[2] vLLM online — served: {served}")
        except Exception as e:
            print(f"\n[2] ERROR: vLLM not reachable at {cosmos_url} — {e}")
            return

        # ── 3. Run all 4 prompts ───────────────────────────────────────────
        print(f"\n[3] Running {len(COSMOS_PROMPTS)} prompts with {cosmos_model}\n")
        print(f"    {'Prompt':<25} {'Time':>6}  {'Chars':>6}  Output")
        print(f"    {'─'*25} {'─'*6}  {'─'*6}  {'─'*40}")

        for prompt_file in COSMOS_PROMPTS:
            slug = prompt_slug(prompt_file)
            template = (PROMPTS_DIR / prompt_file).read_text()
            filled = fill_prompt(template, trajectory_json)

            t0 = time.time()
            try:
                response = await call_cosmos(client, filled, cosmos_model, cosmos_url)
                elapsed = time.time() - t0
            except Exception as e:
                elapsed = time.time() - t0
                print(f"    {slug:<25} {elapsed:>5.1f}s  {'ERR':>6}  FAILED: {e}")
                continue

            # Save .txt (raw response)
            out_txt = COSMOS_OUT / f"cosmos_{slug}_{trace_id}_{ts}.txt"
            out_txt.write_text(response)

            # Save .json (metadata + response)
            out_json = COSMOS_OUT / f"cosmos_{slug}_{trace_id}_{ts}.json"
            meta = {
                "prompt_file":  prompt_file,
                "prompt_slug":  slug,
                "model":        cosmos_model,
                "trace_id":     trace_id,
                "trace_file":   str(trace_path),
                "timestamp":    ts,
                "elapsed_sec":  round(elapsed, 2),
                "response":     response,
            }
            out_json.write_text(json.dumps(meta, indent=2))

            print(f"    {slug:<25} {elapsed:>5.1f}s  {len(response):>6}  {out_txt.name}")

    # ── 4. Summary ─────────────────────────────────────────────────────────
    print(f"\n[4] All done. Results in: outputs/cosmos/")
    print(f"    Files prefixed: cosmos_<prompt>_{trace_id}_{ts}.*\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run all 4 Cosmos prompts on latest screw trace")
    parser.add_argument("--trace",        default=None,         help="Path to trace file (default: latest)")
    parser.add_argument("--cosmos-url",   default=COSMOS_URL,   help="vLLM base URL")
    parser.add_argument("--cosmos-model", default=COSMOS_MODEL, help="Cosmos model ID")
    args = parser.parse_args()

    trace = Path(args.trace) if args.trace else find_latest_trace()
    asyncio.run(main(trace, args.cosmos_model, args.cosmos_url))
