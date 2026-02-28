import asyncio
import json as _json
import os as _os
from contextlib import asynccontextmanager
from datetime import datetime as _datetime
from pathlib import Path as _Path
from typing import Optional

# Workspace root: override with ROBOT_WS env variable, default ~/ros2_ws
_WS_ROOT = _Path(_os.environ.get("ROBOT_WS", _Path.home() / "ros2_ws"))
_COSMOS_OUT_DIR = _WS_ROOT / "outputs" / "cosmos"
_EVAL_OUT_DIR   = _WS_ROOT / "outputs" / "eval"
_COSMOS_OUT_DIR.mkdir(parents=True, exist_ok=True)
_EVAL_OUT_DIR.mkdir(parents=True, exist_ok=True)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .agent_node import AgentNode
from .moveit_client import ARM_JOINTS
from .recorder.execution_trace import JointSnapshot, ExecutionTrace
from .models import (
    MoveJointsRequest,
    MovePoseRequest,
    GripperRequest,
    PlanRequest,
    ExecutePlanRequest,
    SkillRequest,
    RobotStateResponse,
    SkillResultResponse,
    HealthResponse,
    PlanResponse,
    ExplainResponse,
    TraceInfo,
    EvaluateRequest,
    EvaluateResponse,
    AnalyzeRequest,
    AnalyzeResponse,
)
from geometry_msgs.msg import Pose


# Module-level reference set during create_app
_node: Optional[AgentNode] = None


def create_app(node: AgentNode) -> FastAPI:
    global _node
    _node = node

    app = FastAPI(
        title="Robot Agent API",
        description="FastAPI agent for UR5e + Robotiq control with Cosmos Reason2 AI",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- Health ----

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health():
        cosmos_ok = await _node.cosmos.is_available()
        ros_ok = _node.get_current_joint_state() is not None
        ollama_ok = await _node.ollama.is_available()
        return HealthResponse(
            status="ok",
            ros_connected=ros_ok,
            cosmos_available=cosmos_ok,
            ollama_available=ollama_ok,
        )

    # ---- Cosmos Model Info / Switch ----

    @app.get("/api/v1/model")
    async def get_model_info():
        served = await _node.cosmos.get_served_model()
        return {
            "configured_model": _node.cosmos.model,
            "served_model": served,
            "cosmos_url": _node.cosmos.base_url,
            "available": served is not None,
        }

    @app.post("/api/v1/model")
    async def switch_model(model: str = None, cosmos_url: str = None):
        """Switch the Cosmos model or URL at runtime (vLLM server must be serving the new model)."""
        if model:
            _node.cosmos.model = model
        if cosmos_url:
            _node.cosmos._base_url = cosmos_url.rstrip('/')
        served = await _node.cosmos.get_served_model()
        return {
            "configured_model": _node.cosmos.model,
            "served_model": served,
            "cosmos_url": _node.cosmos.base_url,
            "message": "Updated. Make sure your vLLM server is serving this model.",
        }

    # ---- Robot State ----

    @app.get("/api/v1/state", response_model=RobotStateResponse)
    async def get_state():
        js = _node.get_current_joint_state()
        if js is None:
            raise HTTPException(status_code=503, detail="No joint state received yet")
        arm_vals = _node.moveit_client.get_current_joint_values(js)
        name_to_pos = dict(zip(js.name, js.position))
        gripper_pos = name_to_pos.get('rq_robotiq_85_left_knuckle_joint')
        return RobotStateResponse(
            joint_names=list(ARM_JOINTS),
            joint_values=arm_vals or [],
            gripper_position=gripper_pos,
        )

    # ---- Motion Commands ----

    @app.post("/api/v1/move/joints", response_model=SkillResultResponse)
    async def move_joints(req: MoveJointsRequest):
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: _node.moveit_client.plan_and_execute_joints(
                req.joint_values, req.velocity_scaling, req.acceleration_scaling
            ),
        )
        return SkillResultResponse(
            success=ok,
            message="Moved to joint target" if ok else "Failed to plan or execute",
        )

    @app.post("/api/v1/move/pose", response_model=SkillResultResponse)
    async def move_pose(req: MovePoseRequest):
        pose = Pose()
        pose.position.x = req.position.x
        pose.position.y = req.position.y
        pose.position.z = req.position.z
        pose.orientation.x = req.position.qx
        pose.orientation.y = req.position.qy
        pose.orientation.z = req.position.qz
        pose.orientation.w = req.position.qw

        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(
            None,
            lambda: _node.moveit_client.plan_and_execute_pose(
                pose, req.velocity_scaling, req.acceleration_scaling
            ),
        )
        return SkillResultResponse(
            success=ok,
            message="Moved to pose target" if ok else "Failed to plan or execute",
        )

    @app.post("/api/v1/gripper", response_model=SkillResultResponse)
    async def gripper(req: GripperRequest):
        loop = asyncio.get_event_loop()
        if req.action == "open":
            ok = await loop.run_in_executor(None, _node.gripper_client.open)
        elif req.action == "close":
            ok = await loop.run_in_executor(None, _node.gripper_client.close)
        elif req.action == "set":
            if req.position is None:
                raise HTTPException(status_code=400, detail="position required for 'set' action")
            ok = await loop.run_in_executor(
                None, lambda: _node.gripper_client.set_position(req.position, req.max_effort)
            )
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

        return SkillResultResponse(
            success=ok,
            message=f"Gripper {req.action} {'succeeded' if ok else 'failed'}",
        )

    # ---- Skills ----

    @app.get("/api/v1/skills")
    async def list_skills():
        return _node.list_skills()

    @app.post("/api/v1/skill/{skill_name}", response_model=SkillResultResponse)
    async def execute_skill(skill_name: str, req: SkillRequest):
        skill = _node.get_skill(skill_name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"Skill not found: {skill_name}")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: skill.execute_with_recording(req.params)
        )
        return SkillResultResponse(
            success=result.success,
            message=result.message,
            trace_id=result.trace_id,
        )

    # ---- Traces ----

    @app.get("/api/v1/traces")
    async def list_traces():
        return _node.recorder.list_traces()

    @app.get("/api/v1/traces/{trace_id}")
    async def get_trace(trace_id: str):
        trace = _node.recorder.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")
        return {
            "trace_id": trace.trace_id,
            "label": trace.label,
            "duration_sec": trace.duration_sec,
            "num_snapshots": len(trace.snapshots),
            "summary": trace.to_summary_text(),
        }

    # ---- Planner (Cosmos) ----

    @app.post("/api/v1/plan", response_model=PlanResponse)
    async def plan(req: PlanRequest):
        current_state = _node.get_current_state_dict()
        available_skills = _node.list_skills()

        result = await _node.planner.plan(
            instruction=req.instruction,
            available_skills=available_skills,
            current_state=current_state,
        )
        return PlanResponse(plan=result["plan"], reasoning=result["reasoning"])

    @app.post("/api/v1/execute_plan")
    async def execute_plan(req: ExecutePlanRequest):
        results = []
        for step in req.plan:
            skill_name = step.get("skill")
            params = step.get("params", {})

            skill = _node.get_skill(skill_name)
            if skill is None:
                results.append({"skill": skill_name, "success": False, "message": f"Skill not found: {skill_name}"})
                break

            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda s=skill, p=params: s.execute_with_recording(p)
            )
            results.append({
                "skill": skill_name,
                "success": result.success,
                "message": result.message,
                "trace_id": result.trace_id,
            })

            if not result.success:
                break  # Stop on first failure

        return {"results": results}

    # ---- Explainability (Cosmos) ----

    @app.post("/api/v1/explain/{trace_id}", response_model=ExplainResponse)
    async def explain(trace_id: str, task_description: str = ""):
        trace = _node.recorder.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

        explanation = await _node.explainability.explain_trace(
            trace, task_description=task_description or trace.label
        )
        return ExplainResponse(explanation=explanation)

    # ---- Evaluation (Cosmos → Llama via Ollama) ----

    @app.post("/api/v1/evaluate/{trace_id}", response_model=EvaluateResponse)
    async def evaluate(trace_id: str, req: EvaluateRequest):
        """
        Evaluate Cosmos reasoning + joint trajectory using a local Llama model.

        Flow:
          1. Look up the recorded joint-state trace.
          2. If cosmos_reasoning is not provided in the body, call Cosmos first
             to generate an explanation of the trace.
          3. Send both to Llama (via Ollama) with the evaluation rubric.
          4. Return structured scores + overall_score.
        """
        trace = _node.recorder.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

        # Step 1: obtain Cosmos reasoning (generate if not supplied)
        cosmos_reasoning = req.cosmos_reasoning
        if not cosmos_reasoning:
            cosmos_reasoning = await _node.explainability.explain_trace(
                trace,
                task_description=req.task_description or trace.label,
            )

        # Step 2: evaluate with Llama
        result = await _node.evaluator.evaluate(
            trace=trace,
            cosmos_reasoning_text=cosmos_reasoning,
            task_description=req.task_description,
            object_name=req.object_name,
            pick_position=req.pick_position,
            place_position=req.place_position,
            robot_description=req.robot_description,
        )

        return EvaluateResponse(**result)

    # ---- Ollama / Evaluator info ----

    @app.get("/api/v1/ollama")
    async def ollama_info():
        available = await _node.ollama.is_available()
        models = await _node.ollama.list_models() if available else []
        return {
            "ollama_url": _node.ollama.base_url,
            "configured_model": _node.ollama.model,
            "available": available,
            "local_models": models,
        }

    # ---- Traces: load from disk ----

    @app.post("/api/v1/traces/load")
    async def load_trace_from_disk(file_path: str):
        """
        Load a pick_place_box_*.json file from disk into the in-memory trace store.
        Accepts both formats:
          - snapshots format: {"snapshots": [{timestamp, joint_names, positions, velocities}]}
          - trajectory format: {"trajectory": [{t, joints}]}
        Returns the trace_id so you can pass it to /analyze or /evaluate.
        """
        p = _Path(file_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

        with open(p) as f:
            data = _json.load(f)

        if "snapshots" in data and data["snapshots"]:
            # Format saved by test_pick_place_box.py
            raw = data["snapshots"]
            snapshots = [
                JointSnapshot(
                    timestamp=float(s["timestamp"]),
                    joint_names=s["joint_names"],
                    positions=[float(v) for v in s["positions"]],
                    velocities=[float(v) for v in s.get("velocities", [0.0] * len(s["positions"]))],
                )
                for s in raw
            ]
            start_time = float(raw[0]["timestamp"])
            end_time   = float(raw[-1]["timestamp"])
        elif "trajectory" in data and data["trajectory"]:
            # Legacy trajectory format
            trajectory = data["trajectory"]
            joint_names = list(trajectory[0]["joints"].keys())
            base_time = 1_700_000_000.0
            snapshots = [
                JointSnapshot(
                    timestamp=base_time + float(row["t"]),
                    joint_names=joint_names,
                    positions=[float(row["joints"].get(j, 0.0)) for j in joint_names],
                    velocities=[0.0] * len(joint_names),
                )
                for row in trajectory
            ]
            start_time = base_time + float(trajectory[0]["t"])
            end_time   = base_time + float(trajectory[-1]["t"])
        else:
            raise HTTPException(status_code=400, detail="No snapshots or trajectory data in file")

        trace = ExecutionTrace(
            trace_id=data.get("trace_id", p.stem),
            label=data.get("label", "loaded"),
            snapshots=snapshots,
            start_time=start_time,
            end_time=end_time,
        )
        _node.recorder.traces[trace.trace_id] = trace
        return {
            "trace_id":     trace.trace_id,
            "label":        trace.label,
            "snapshots":    len(snapshots),
            "duration_sec": round(trace.duration_sec, 2),
        }

    # ---- Analyze: Prompt 1 (Cosmos) → Prompt 2 (Llama judge) ----

    @app.post("/api/v1/analyze/{trace_id}", response_model=AnalyzeResponse)
    async def analyze(trace_id: str, req: AnalyzeRequest):
        """
        Full pipeline:
          1. Look up the trace in the recorder.
          2. Send trace + Prompt 1 to Cosmos Reason2 for trajectory analysis.
             (skipped if req.cosmos_analysis is provided)
          3. Send Cosmos output + Prompt 2 to Llama for judging.
          4. Return both Cosmos analysis text and Llama scores.
        """
        trace = _node.recorder.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=f"Trace not found: {trace_id}")

        # Step 1: Cosmos analysis (Prompt 1)
        cosmos_analysis = req.cosmos_analysis
        if not cosmos_analysis:
            cosmos_ok = await _node.cosmos.is_available()
            if not cosmos_ok:
                raise HTTPException(
                    status_code=503,
                    detail="Cosmos server not available. Start vLLM or supply cosmos_analysis in body.",
                )
            cosmos_analysis = await _node.explainability.analyze_trace(
                trace=trace,
                instruction=req.instruction,
                object_name=req.object_name,
                pick_x=req.pick_x,
                pick_y=req.pick_y,
                pick_z=req.pick_z,
                object_dims=req.object_dims,
                grasp_z=req.grasp_z,
                approach_height=req.approach_height,
                retreat_height=req.retreat_height,
                grasp_gripper_rad=req.grasp_gripper_rad,
                place_description=req.place_description,
                scene_context=req.scene_context,
            )

        # Step 2: Llama judge (Prompt 2)
        result = await _node.evaluator.evaluate(
            trace=trace,
            cosmos_reasoning_text=cosmos_analysis,
            task_description=req.instruction,
            object_name=req.object_name,
            pick_position=f"[{req.pick_x:.3f}, {req.pick_y:.3f}, {req.pick_z:.3f}]",
            place_position=req.place_description,
            grasp_z=req.grasp_z,
            grasp_gripper_rad=req.grasp_gripper_rad,
        )

        # Auto-save outputs with datetime-stamped filenames
        ts = _datetime.now().strftime("%Y%m%d_%H%M%S")
        cosmos_file = _COSMOS_OUT_DIR / f"cosmos_{trace.trace_id}_{ts}.txt"
        cosmos_file.write_text(cosmos_analysis)
        eval_payload = {k: v for k, v in result.items() if k not in ("trace_id", "trace_label")}
        eval_file = _EVAL_OUT_DIR / f"eval_{trace.trace_id}_{ts}.json"
        eval_file.write_text(_json.dumps(eval_payload, indent=2))

        return AnalyzeResponse(
            trace_id=trace.trace_id,
            trace_label=trace.label,
            cosmos_analysis=cosmos_analysis,
            overall_score=result.get("overall_score", 0.0),
            task_outcome=result.get("task_outcome", "uncertain"),
            spatial_awareness=result.get("spatial_awareness"),
            plan_coherence=result.get("plan_coherence"),
            safety_awareness=result.get("safety_awareness"),
            goal_achievement=result.get("goal_achievement"),
            anomaly_detection=result.get("anomaly_detection"),
            corrective_feedback=result.get("corrective_feedback"),
            consistency=result.get("consistency"),
            critical_failures=result.get("critical_failures", []),
            suggestions=result.get("suggestions", []),
            parse_error=result.get("parse_error"),
        )

    return app
