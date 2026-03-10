#!/usr/bin/env python3
"""
Batch evaluation of the screwing trace against all prompt / model combinations.

Test matrix  (8 runs total)
----------------------------
Each run is a full Cosmos → Llama pipeline.
Cosmos prompts and Llama eval prompts are paired by index (not all combinations).

  Cosmos 2B  (runs 1–4)
  ┌─────────────────────────────────────────────────────────┐
  │ Run 1: cosmos_differential   → eval_checklist           │
  │ Run 2: cosmos_kinematic      → eval_pairwise            │
  │ Run 3: cosmos_phases         → eval_rubric              │
  │ Run 4: cosmos_statemachine   → eval_temporal            │
  └─────────────────────────────────────────────────────────┘

  Cosmos 8B  (runs 5–8)
  ┌─────────────────────────────────────────────────────────┐
  │ Run 5: cosmos_differential   → eval_checklist           │
  │ Run 6: cosmos_kinematic      → eval_pairwise            │
  │ Run 7: cosmos_phases         → eval_rubric              │
  │ Run 8: cosmos_statemachine   → eval_temporal            │
  └─────────────────────────────────────────────────────────┘

Usage
-----
  # Make sure robot_agent is running first:
  #   ros2 run robot_agent robot_agent
  python3 ~/ros2_ws/tests/batch_screw_eval.py [--api http://localhost:8080]
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRACE_FILE = (
    Path(__file__).parent.parent
    / "outputs" / "screw_trace"
    / "screw_continuous_20260308_115305.json"
)

COSMOS_MODELS = [
    "nvidia/Cosmos-Reason2-2B",
    "nvidia/Cosmos-Reason2-8B",
]

# Paired by index: each cosmos prompt is evaluated by the matching eval prompt
COSMOS_PROMPTS = [
    "cosmos_screwing_differential.txt",
    "cosmos_screwing_kinematic.txt",
    "cosmos_screwing_phases.txt",
    "cosmos_screwing_statemachine.txt",
]

EVAL_PROMPTS = [
    "eval_screwing_checklist.txt",
    "eval_screwing_pairwise.txt",
    "eval_screwing_rubric.txt",
    "eval_screwing_temporal.txt",
]

PROMPT_PAIRS = list(zip(COSMOS_PROMPTS, EVAL_PROMPTS))

# Task context forwarded to both Cosmos and Llama
TASK_CONTEXT = dict(
    instruction="Pick the screwdriver and drive it continuously into the screw",
    object_name="screwdriver",
    pick_x=0.45,
    pick_y=0.00,
    pick_z=0.09,
    grasp_z=0.26,
    grasp_gripper_rad=0.57,
    object_dims="cylinder: diameter 24 mm, height 180 mm (upright)",
    approach_height=0.18,
    retreat_height=0.20,
    place_description="screw at x=0.40, y=0.10",
    scene_context=(
        "Flat table. Screwdriver upright at x=0.45, y=0.00. "
        "Screw target at x=0.40, y=0.10. No other obstacles. "
        "Robot base at world origin. Wrist-3 rotates CW to drive screw, "
        "lifts to reposition CCW, then re-engages for next stroke."
    ),
)

OUTPUT_DIR = Path(__file__).parent.parent / "outputs" / "batch_eval"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def short_name(prompt_file: str) -> str:
    return prompt_file.replace(".txt", "").replace("cosmos_screwing_", "").replace("eval_screwing_", "")


def model_tag(model: str) -> str:
    return "2B" if "2B" in model else "8B"


async def load_trace(client: httpx.AsyncClient, api: str) -> str:
    resp = await client.post(
        f"{api}/api/v1/traces/load",
        params={"file_path": str(TRACE_FILE)},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  Trace loaded: id={data['trace_id']}  "
          f"snapshots={data['snapshots']}  duration={data['duration_sec']:.1f}s")
    return data["trace_id"]


async def switch_cosmos_model(client: httpx.AsyncClient, api: str, model: str) -> None:
    resp = await client.post(
        f"{api}/api/v1/model",
        params={"model": model},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"  Cosmos model → {data['configured_model']}  (served: {data['served_model']})")


async def run_analyze(
    client: httpx.AsyncClient,
    api: str,
    trace_id: str,
    cosmos_prompt: str,
    eval_prompt: str,
    timeout: float = 600,
) -> dict:
    body = {
        **TASK_CONTEXT,
        "cosmos_prompt": cosmos_prompt,
        "eval_prompt":   eval_prompt,
    }

    resp = await client.post(
        f"{api}/api/v1/analyze/{trace_id}",
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(api: str, models: list[str] = COSMOS_MODELS) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary_path = OUTPUT_DIR / f"batch_{ts_label}.json"

    results: list[dict] = []

    async with httpx.AsyncClient() as client:

        # ------------------------------------------------------------------ #
        # 0. Health check
        # ------------------------------------------------------------------ #
        print("\n=== Health check ===")
        try:
            health = (await client.get(f"{api}/api/v1/health", timeout=10)).json()
            print(f"  ROS: {health['ros_connected']}  "
                  f"Cosmos: {health['cosmos_available']}  "
                  f"Ollama: {health['ollama_available']}")
            if not health["cosmos_available"]:
                print("  WARNING: Cosmos not available — cosmos runs will fail.")
            if not health["ollama_available"]:
                print("  WARNING: Ollama not available — eval runs will fail.")
        except Exception as exc:
            print(f"  Health check failed: {exc}")
            print("  Is robot_agent running?  ros2 run robot_agent robot_agent")
            sys.exit(1)

        # ------------------------------------------------------------------ #
        # 1. Load trace
        # ------------------------------------------------------------------ #
        print("\n=== Load trace ===")
        trace_id = await load_trace(client, api)

        # ------------------------------------------------------------------ #
        # 2. Eight runs: 2 models × 4 paired (cosmos_prompt, eval_prompt)
        # ------------------------------------------------------------------ #
        total_runs = len(models) * len(PROMPT_PAIRS)
        run_idx = 0

        for model in models:
            print(f"\n{'='*66}")
            print(f"  Cosmos model: {model}")
            print(f"{'='*66}")

            await switch_cosmos_model(client, api, model)

            for c_prompt, e_prompt in PROMPT_PAIRS:
                run_idx += 1
                tag = f"{model_tag(model)}-{short_name(c_prompt)}-{short_name(e_prompt)}"
                print(f"\n[{run_idx:2d}/{total_runs}] {tag}")
                print(f"  cosmos_prompt = {c_prompt}")
                print(f"  eval_prompt   = {e_prompt}")

                t0 = time.monotonic()
                try:
                    data = await run_analyze(
                        client, api, trace_id,
                        cosmos_prompt=c_prompt,
                        eval_prompt=e_prompt,
                    )
                    elapsed = time.monotonic() - t0
                    score   = data.get("overall_score", "?")
                    outcome = data.get("task_outcome", "?")
                    print(f"  → score={score}  outcome={outcome}  ({elapsed:.1f}s)")

                    results.append({
                        "run":           tag,
                        "cosmos_model":  model,
                        "cosmos_prompt": c_prompt,
                        "eval_prompt":   e_prompt,
                        "elapsed_sec":   round(elapsed, 1),
                        **data,
                    })

                except Exception as exc:
                    elapsed = time.monotonic() - t0
                    print(f"  ✗ FAILED: {exc}  ({elapsed:.1f}s)")
                    results.append({
                        "run":           tag,
                        "cosmos_model":  model,
                        "cosmos_prompt": c_prompt,
                        "eval_prompt":   e_prompt,
                        "elapsed_sec":   round(elapsed, 1),
                        "error":         str(exc),
                    })

                # Save after every run so partial results are not lost
                summary_path.write_text(json.dumps(results, indent=2))

        # ------------------------------------------------------------------ #
        # 4. Summary table
        # ------------------------------------------------------------------ #
        print(f"\n{'='*66}")
        print("  SUMMARY")
        print(f"{'='*66}")
        header = f"{'Run':<40}  {'Score':>6}  {'Outcome':<12}  {'Time':>6}"
        print(header)
        print("-" * len(header))
        for r in results:
            score   = r.get("overall_score", r.get("error", "ERR"))
            outcome = r.get("task_outcome", "error")
            elapsed = r.get("elapsed_sec", 0)
            print(f"  {r['run']:<38}  {str(score):>6}  {outcome:<12}  {elapsed:>5.0f}s")

        print(f"\n  Results saved → {summary_path}")
        print(f"  Total runs: {len(results)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch screwing trace evaluation")
    parser.add_argument(
        "--api",
        default="http://localhost:8080",
        help="robot_agent API base URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--models",
        default="2B,8B",
        help="Comma-separated list of model sizes to run, e.g. 2B  or  8B  or  2B,8B (default: 2B,8B)",
    )
    args = parser.parse_args()

    selected = [m.strip().upper() for m in args.models.split(",")]
    active_models = [m for m in COSMOS_MODELS if any(s in m for s in selected)]
    if not active_models:
        print(f"No matching models for --models={args.models}")
        sys.exit(1)

    asyncio.run(main(args.api, active_models))
