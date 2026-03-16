from pydantic import BaseModel, Field
from typing import List, Optional
from dataclasses import dataclass


class MoveJointsRequest(BaseModel):
    joint_values: List[float] = Field(..., min_length=6, max_length=6)
    velocity_scaling: float = Field(0.3, ge=0.01, le=1.0)
    acceleration_scaling: float = Field(0.3, ge=0.01, le=1.0)


class PoseDict(BaseModel):
    x: float
    y: float
    z: float
    qx: float = 0.0
    qy: float = 0.0
    qz: float = 0.0
    qw: float = 1.0


class MovePoseRequest(BaseModel):
    position: PoseDict
    velocity_scaling: float = 0.3
    acceleration_scaling: float = 0.3


class GripperRequest(BaseModel):
    action: str = Field(..., pattern=r"^(open|close|set)$")
    position: Optional[float] = None
    max_effort: float = 0.0


class PlanRequest(BaseModel):
    instruction: str


class ExecutePlanRequest(BaseModel):
    plan: List[dict]


class SkillRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class RobotStateResponse(BaseModel):
    joint_names: List[str]
    joint_values: List[float]
    gripper_position: Optional[float] = None


class SkillResultResponse(BaseModel):
    success: bool
    message: str
    trace_id: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    ros_connected: bool
    cosmos_available: bool
    ollama_available: bool = False
    cosmos_2b_available: bool = False
    cosmos_8b_available: bool = False


class PlanResponse(BaseModel):
    plan: List[dict]
    reasoning: str = ""


class ExplainResponse(BaseModel):
    explanation: str
    reasoning_tokens: str = ""


class TraceInfo(BaseModel):
    trace_id: str
    label: str
    duration_sec: float
    num_snapshots: int


class EvaluateRequest(BaseModel):
    task_description: str = ""
    object_name: str = ""
    pick_position: str = ""
    place_position: str = ""
    robot_description: str = "UR5e 6-DOF manipulator with Robotiq 2F-85 parallel gripper"
    # If supplied, skip the Cosmos call and use this text directly.
    cosmos_reasoning: Optional[str] = None


class EvaluateResponse(BaseModel):
    trace_id: str
    trace_label: str
    overall_score: float
    spatial_awareness: Optional[dict] = None
    plan_coherence: Optional[dict] = None
    safety_awareness: Optional[dict] = None
    goal_achievement: Optional[dict] = None
    efficiency: Optional[dict] = None
    smoothness: Optional[dict] = None
    consistency: Optional[dict] = None
    critical_failures: List[str] = []
    suggestions: List[str] = []
    parse_error: Optional[str] = None
    raw_response: Optional[str] = None


# ── Analyze endpoint (Prompt 1 → Cosmos, Prompt 2 → Llama) ────────────────

class AnalyzeRequest(BaseModel):
    instruction: str = "Pick the screwdriver from the table"
    object_name: str = "screwdriver"
    pick_x: float = 0.45
    pick_y: float = 0.00
    pick_z: float = 0.09          # object CoM height
    object_dims: str = "cylinder: diameter 24 mm, height 180 mm (upright)"
    grasp_z: float = 0.26         # tool0 target z at grasp
    approach_height: float = 0.18
    retreat_height: float = 0.18
    grasp_gripper_rad: float = 0.57
    grasp_quaternion: str = "1.0, 0.0, 0.0, 0.0"   # qx, qy, qz, qw (top-down)
    place_description: str = "N/A — pick only"
    scene_context: str = (
        "Flat table. Screwdriver upright in MoveIt planning scene at x=0.45, y=0.00. "
        "No other obstacles. Robot base at world origin."
    )
    # Screwing-specific parameters (used by checklist / pairwise / temporal prompts)
    screw_x: float = 0.40
    screw_y: float = 0.10
    n_cycles: int = 5
    wrist3_safe_min: float = -5.983   # -(2π - 0.3)
    wrist3_safe_max: float = 5.983    #  (2π - 0.3)
    screw_step_rad: float = -0.785    # -π/4
    # Reference text for pairwise eval prompt (leave empty to auto-skip)
    reference_analysis: str = ""
    # If provided, skip the Cosmos call (useful for testing Llama judge alone)
    cosmos_analysis: Optional[str] = None
    # Per-call prompt overrides (filename within prompts/ directory)
    cosmos_prompt: Optional[str] = None
    eval_prompt: Optional[str] = None


class AnalyzeResponse(BaseModel):
    trace_id: str
    trace_label: str
    # Cosmos output (Prompt 1)
    cosmos_analysis: str
    # Llama judge output (Prompt 2)
    overall_score: float
    task_outcome: str = "uncertain"
    spatial_awareness: Optional[dict] = None
    plan_coherence: Optional[dict] = None
    safety_awareness: Optional[dict] = None
    goal_achievement: Optional[dict] = None
    anomaly_detection: Optional[dict] = None
    corrective_feedback: Optional[dict] = None
    consistency: Optional[dict] = None
    critical_failures: List[str] = []
    suggestions: List[str] = []
    parse_error: Optional[str] = None
    # Full Llama output preserved for non-standard prompt formats
    # (checklist trajectory_checks, temporal event_alignment, pairwise verdicts, etc.)
    raw_result: Optional[dict] = None
