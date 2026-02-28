#!/usr/bin/env python3
"""
Test the /api/v1/evaluate endpoint via the FastAPI HTTP layer.

Uses httpx's ASGI transport — no real server port, no ROS2 needed.
Requires Ollama running locally with at least one model pulled.

Usage:
    python3 test_evaluation_fastapi.py
    python3 test_evaluation_fastapi.py --model llama3:latest --pass-reasoning
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src" / "robot_agent"))

import httpx
from fastapi import FastAPI, HTTPException

from robot_agent.cosmos.ollama_client import OllamaClient
from robot_agent.cosmos.evaluator import EvaluationEngine
from robot_agent.recorder.execution_trace import ExecutionTrace, JointSnapshot
from robot_agent.models import EvaluateRequest, EvaluateResponse

# ---------------------------------------------------------------------------
# Mock data (same as test_evaluation.py)
# ---------------------------------------------------------------------------

MOCK_COSMOS_REASONING = """
The robot is performing a pick-and-place task involving a red cube located at
approximately (0.40, 0.10, 0.05) in the robot's base frame.

Step 1 – Pre-grasp approach:
The arm moves from its home configuration to a position directly above the
object. The shoulder_lift joint rotates to roughly -1.2 rad and the elbow
joint to ~1.4 rad, bringing the wrist above the target at a safe clearance.

Step 2 – Descent:
wrist_1 and wrist_2 are adjusted so the gripper faces downward. The arm
descends by increasing shoulder_lift by ~0.3 rad while keeping wrist
orientation fixed.

Step 3 – Grasp:
The Robotiq 2F-85 gripper closes from 0.0 to 0.79 rad to secure the cube.

Step 4 – Lift and transport:
The arm lifts ~10 cm and shoulder_pan rotates approximately -0.6 rad to sweep
toward the goal position at (0.20, -0.30, 0.30).

Step 5 – Placement:
The arm descends to shelf height, releases the gripper, and returns home.
""".strip()

_JOINT_NAMES = [
    "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
    "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
    "rq_robotiq_85_left_knuckle_joint",
]

_WAYPOINTS = [
    (0.0,  [0.000, -1.571, 0.000, -1.571,  0.000, 0.000, 0.00]),
    (1.0,  [0.150, -1.200, 1.400, -1.750, -1.571, 0.000, 0.00]),
    (2.0,  [0.150, -0.900, 1.400, -2.050, -1.571, 0.000, 0.00]),
    (2.5,  [0.150, -0.900, 1.400, -2.050, -1.571, 0.000, 0.79]),
    (3.0,  [0.150, -1.200, 1.400, -1.750, -1.571, 0.000, 0.79]),
    (4.0,  [-0.45, -1.100, 1.300, -1.800, -1.571, 0.000, 0.79]),
    (5.0,  [-0.45, -0.850, 1.300, -2.000, -1.571, 0.000, 0.79]),
    (5.5,  [-0.45, -0.850, 1.300, -2.000, -1.571, 0.000, 0.00]),
    (6.5,  [0.000, -1.571, 0.000, -1.571,  0.000, 0.000, 0.00]),
]

TRACE_ID = "fastapi-test-001"


def _build_mock_trace() -> ExecutionTrace:
    base = 1_700_000_000.0
    snapshots = [
        JointSnapshot(
            timestamp=base + t,
            joint_names=_JOINT_NAMES,
            positions=pos,
            velocities=[0.0] * len(_JOINT_NAMES),
        )
        for t, pos in _WAYPOINTS
    ]
    return ExecutionTrace(
        trace_id=TRACE_ID,
        label="pick_red_cube",
        snapshots=snapshots,
        start_time=base,
        end_time=base + _WAYPOINTS[-1][0],
    )


# ---------------------------------------------------------------------------
# Minimal mock node (only the attributes the evaluate endpoint touches)
# ---------------------------------------------------------------------------

class _MockExplainability:
    """Returns fixed reasoning so we don't need Cosmos for this test."""
    async def explain_trace(self, trace, task_description=""):
        return MOCK_COSMOS_REASONING


class _MockRecorder:
    def __init__(self, trace: ExecutionTrace):
        self._traces = {trace.trace_id: trace}

    def get_trace(self, trace_id: str):
        return self._traces.get(trace_id)


class MockNode:
    def __init__(self, ollama: OllamaClient):
        trace = _build_mock_trace()
        self.recorder = _MockRecorder(trace)
        self.ollama = ollama
        self.evaluator = EvaluationEngine(ollama)
        self.explainability = _MockExplainability()


# ---------------------------------------------------------------------------
# Minimal FastAPI app — only the evaluate endpoint
# ---------------------------------------------------------------------------

def build_test_app(node: MockNode) -> FastAPI:
    app = FastAPI(title="Evaluate Test")

    @app.post("/api/v1/evaluate/{trace_id}", response_model=EvaluateResponse)
    async def evaluate(trace_id: str, req: EvaluateRequest):
        trace = node.recorder.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

        cosmos_reasoning = req.cosmos_reasoning
        if not cosmos_reasoning:
            cosmos_reasoning = await node.explainability.explain_trace(
                trace, task_description=req.task_description or trace.label
            )

        result = await node.evaluator.evaluate(
            trace=trace,
            cosmos_reasoning_text=cosmos_reasoning,
            task_description=req.task_description,
            object_name=req.object_name,
            pick_position=req.pick_position,
            place_position=req.place_position,
            robot_description=req.robot_description,
        )
        return EvaluateResponse(**result)

    @app.get("/api/v1/ollama")
    async def ollama_info():
        available = await node.ollama.is_available()
        models = await node.ollama.list_models() if available else []
        return {
            "ollama_url": node.ollama.base_url,
            "configured_model": node.ollama.model,
            "available": available,
            "local_models": models,
        }

    return app


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _print_response(data: dict):
    print("\n" + "=" * 65)
    print("  HTTP RESPONSE — /api/v1/evaluate")
    print("=" * 65)

    dims = [
        ("goal_achievement",  "Goal Achievement "),
        ("consistency",       "Consistency      "),
        ("plan_coherence",    "Plan Coherence   "),
        ("smoothness",        "Smoothness       "),
        ("safety_awareness",  "Safety Awareness "),
        ("efficiency",        "Efficiency       "),
        ("spatial_awareness", "Spatial Awareness"),
    ]
    for key, label in dims:
        dim = data.get(key) or {}
        score = dim.get("score", "?")
        just = dim.get("justification", "")
        bar = "█" * (score * 4) + "░" * (20 - score * 4) if isinstance(score, int) else ""
        print(f"\n  {label}  [{bar}] {score}/5")
        if just:
            print(f"    {just}")

    print(f"\n{'─' * 65}")
    print(f"  OVERALL SCORE : {data.get('overall_score', '?')} / 5.000")
    print(f"{'─' * 65}")

    failures = data.get("critical_failures") or []
    if failures and failures != ["None"]:
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

    print("=" * 65)


async def run(ollama_url: str, model: str, pass_reasoning: bool):
    print(f"\nOllama URL : {ollama_url}")
    print(f"Model      : {model}")
    print(f"Pre-supply cosmos_reasoning in request body: {pass_reasoning}")

    ollama = OllamaClient(base_url=ollama_url, model=model)

    # 1. Connectivity
    print("\n[1/4] Checking Ollama...")
    if not await ollama.is_available():
        print(f"  ERROR: Ollama not reachable at {ollama_url}")
        return
    models = await ollama.list_models()
    print(f"  OK — local models: {models}")

    # 2. Build app
    print("\n[2/4] Building FastAPI test app + mock node...")
    node = MockNode(ollama)
    app = build_test_app(node)
    print(f"  Trace '{TRACE_ID}' pre-seeded into mock recorder.")

    # 3. Fire request via ASGI transport (no real port)
    print(f"\n[3/4] POST /api/v1/evaluate/{TRACE_ID} via ASGI transport...")

    payload = EvaluateRequest(
        task_description="Pick red cube from table and place it on the shelf",
        object_name="red_cube",
        pick_position="[0.40, 0.10, 0.05]",
        place_position="[0.20, -0.30, 0.30]",
        robot_description="UR5e 6-DOF manipulator with Robotiq 2F-85 parallel gripper",
        cosmos_reasoning=MOCK_COSMOS_REASONING if pass_reasoning else None,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        # Sanity check — /api/v1/ollama
        r = await client.get("/api/v1/ollama")
        print(f"  GET /api/v1/ollama → {r.status_code}: {r.json()}")

        print(f"\n  Sending evaluate request (may take 30–90 s)...")
        r = await client.post(
            f"/api/v1/evaluate/{TRACE_ID}",
            json=payload.model_dump(),
            timeout=180.0,
        )
        print(f"  Response status : {r.status_code}")

    if r.status_code != 200:
        print(f"  ERROR body: {r.text}")
        return

    data = r.json()

    # 4. Print
    print("\n[4/4] Results:")
    _print_response(data)

    print("\nFull JSON response:")
    print(json.dumps(data, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Test /api/v1/evaluate via FastAPI ASGI transport"
    )
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--model", default="llama3:latest")
    parser.add_argument(
        "--pass-reasoning", action="store_true",
        help="Supply cosmos_reasoning in the request body (skips mock Cosmos call)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.ollama_url, args.model, args.pass_reasoning))


if __name__ == "__main__":
    main()
