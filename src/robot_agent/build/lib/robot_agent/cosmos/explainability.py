import json

from ..recorder.execution_trace import ExecutionTrace
from .cosmos_client import CosmosClient


# ---------------------------------------------------------------------------
# PROMPT 1 — sent to Cosmos Reason2
# Role: Deep trajectory analysis, phase segmentation, success assessment,
#       corrective feedback, replanning waypoints.
# UR5e + Robotiq 2F-85 config is pre-filled in the system prompt so the
# user message only needs task-specific values.
# ---------------------------------------------------------------------------

_COSMOS_SYSTEM_PROMPT = """\
You are a robotic manipulation reasoning model specialised in analysing UR5e \
joint trajectories. You observe recorded joint states, segment the motion into \
semantic phases, assess task success, and provide concrete corrective feedback \
or replanning waypoints.

## Robot Configuration
- **Arm**: Universal Robots UR5e — 6-DOF serial manipulator, 850 mm reach, 5 kg payload
- **Joints** (in order, all in radians, limits ±6.28 rad):
    1. shoulder_pan_joint   — base rotation  (+ = counter-clockwise from above)
    2. shoulder_lift_joint  — upper-arm pitch (−π/2 ≈ arm horizontal forward)
    3. elbow_joint          — forearm pitch   (+ folds toward shoulder)
    4. wrist_1_joint        — wrist pitch
    5. wrist_2_joint        — wrist roll
    6. wrist_3_joint        — tool rotation
- **Home configuration**: [0, −1.571, 0, −1.571, 0, 0] rad
  → arm upright, end-effector facing straight down, ready to reach forward
- **End-effector link**: tool0

## Gripper
- **Model**: Robotiq 2F-85 parallel-finger gripper
- **Recorded joint**: rq_robotiq_85_left_knuckle_joint
  - 0.00 rad → fully open  (85 mm between finger tips)
  - 0.79 rad → fully closed (0 mm gap)
  - ~0.57 rad → ~24 mm gap (matches a standard screwdriver shaft)
- **Finger-tip offset**: ~0.17 m below tool0 when fully open
- **Top-down grasp orientation**: qx=1, qy=0, qz=0, qw=0 (tool0 Z pointing toward −world_Z)

## Trajectory Format
The trajectory is a JSON list of objects {{t, joints}} where:
- t      = seconds elapsed since task start
- joints = mapping of joint_name → angle in radians (arm joints + gripper)
A stationary segment (all joints constant across many rows) typically means
the controller is planning, waiting for IK, or the gripper is actuating."""


_COSMOS_USER_TEMPLATE = """\
## Task
- **Instruction**: {instruction}
- **Object to pick**: {object_name}
  - Position in base_link frame: x={pick_x:.3f} m, y={pick_y:.3f} m, z={pick_z:.3f} m (object CoM)
  - Object dimensions: {object_dims}
- **Target tool0 grasp height**: z = {grasp_z:.3f} m \
(finger tips ≈ {fingertip_z:.3f} m — should align with object CoM)
- **Approach height** (tool0 above object before descending): z = {approach_z:.3f} m
- **Retreat height**  (tool0 after grasping, lifting clear): z = {retreat_z:.3f} m
- **Expected gripper position at grasp**: ≈ {grasp_gripper_rad:.2f} rad
- **Place target**: {place_description}
- **Scene context**: {scene_context}

## Recorded Joint Trajectory
```json
{trajectory_json}
```

---

## Your Analysis

### 1. Phase Segmentation
Identify each semantic phase in the trajectory. For each provide:
- **Phase name** (e.g. idle / approach / descend / grasp / retreat)
- **Time range** (t_start … t_end s)
- **Dominant joint movements** (which joints moved significantly and by how much)
- **Gripper state** at the start and end of the phase (open / closing / closed / value in rad)

### 2. Task Success Assessment
For each question answer Yes / No / Uncertain, and cite evidence from the trajectory:
a) Did the arm reach the approach position (tool0 z ≈ {approach_z:.3f} m) before descending?
b) Did the gripper close at approximately the correct grasp height (tool0 z ≈ {grasp_z:.3f} m)?
c) Was the gripper closing position ≈ {grasp_gripper_rad:.2f} rad (matching object width)?
d) Did the arm successfully retreat with the object after grasping?
e) Were there anomalies (flat/stationary segments, sudden joint jumps >0.3 rad per timestep, \
gripper state changes at unexpected heights)?

### 3. Spatial Reasoning
Describe how tool0 moved through Cartesian space. Was the descent vertical? \
Was the approach angle appropriate for a top-down pick? \
Was the motion efficient, or were there unnecessary detours? \
Estimate key Cartesian positions from the joint values where possible.

### 4. Corrective Feedback
If the task failed or was suboptimal, provide numbered, specific corrections with concrete values \
(e.g. "Increase grasp_z from {grasp_z:.3f} to {corrected_z:.3f} m to account for finger-tip offset"). \
If no corrections are needed write: **No corrections required.**

### 5. Revised Waypoints
If replanning is needed output waypoints as JSON (omit this section if task succeeded):
```json
[
  {{
    "phase": "<name>",
    "joints": [shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3],
    "gripper_rad": <0.0–0.79>,
    "note": "<rationale>"
  }}
]
```
If the task succeeded write: **Task succeeded — no replanning required.**"""


class ExplainabilityEngine:
    """Sends execution traces to Cosmos Reason2 for deep trajectory analysis."""

    def __init__(self, cosmos_client: CosmosClient):
        self._cosmos = cosmos_client

    # ------------------------------------------------------------------
    # Primary: full structured analysis (Prompt 1 — sent to Cosmos)
    # ------------------------------------------------------------------

    async def analyze_trace(
        self,
        trace: ExecutionTrace,
        instruction: str = "Pick the screwdriver from the table",
        object_name: str = "screwdriver",
        pick_x: float = 0.45,
        pick_y: float = 0.00,
        pick_z: float = 0.09,
        object_dims: str = "cylinder: diameter 24 mm, height 180 mm (upright)",
        grasp_z: float = 0.26,
        approach_height: float = 0.18,
        retreat_height: float = 0.18,
        grasp_gripper_rad: float = 0.57,
        place_description: str = "N/A — pick only",
        scene_context: str = (
            "Flat table. Screwdriver upright in MoveIt planning scene at x=0.45, y=0.00. "
            "No other obstacles. Robot base is at world origin."
        ),
        max_trajectory_rows: int = 15,
    ) -> str:
        """
        Format the trace with Prompt 1 and send to Cosmos.
        Returns Cosmos's full reasoning text.
        """
        fingertip_z  = grasp_z - 0.17
        approach_z   = grasp_z + approach_height
        retreat_z    = grasp_z + retreat_height
        corrected_z  = grasp_z + 0.02  # used in the corrective feedback placeholder

        trajectory_json = json.dumps(
            trace.to_trajectory_json(max_rows=max_trajectory_rows), indent=2
        )

        user_msg = _COSMOS_USER_TEMPLATE.format(
            instruction=instruction,
            object_name=object_name,
            pick_x=pick_x, pick_y=pick_y, pick_z=pick_z,
            object_dims=object_dims,
            grasp_z=grasp_z,
            fingertip_z=fingertip_z,
            approach_z=approach_z,
            retreat_z=retreat_z,
            grasp_gripper_rad=grasp_gripper_rad,
            place_description=place_description,
            scene_context=scene_context,
            trajectory_json=trajectory_json,
            corrected_z=corrected_z,
        )

        messages = [
            {"role": "system", "content": _COSMOS_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ]

        return await self._cosmos.chat_completion(messages, max_tokens=3000)

    # ------------------------------------------------------------------
    # Legacy: lightweight summary explanation (kept for /explain endpoint)
    # ------------------------------------------------------------------

    async def explain_trace(self, trace: ExecutionTrace, task_description: str = "") -> str:
        """Simple explanation using text summary (used by legacy /explain endpoint)."""
        _LEGACY_SYSTEM = (
            "You are an expert robot motion analyst. Analyse joint state traces "
            "from a UR5e robot arm with a Robotiq 2F-85 gripper and explain "
            "what the robot did, why, and what the next action should be. "
            "Joints are in radians; gripper 0.0=open, 0.79=closed."
        )
        summary = trace.to_summary_text(max_rows=20)
        user_msg = (
            f"Task: {task_description}\n\nExecution trace:\n{summary}\n\n"
            "What did the robot do and why? What can be the next immediate action?"
        )
        messages = [
            {"role": "system", "content": _LEGACY_SYSTEM},
            {"role": "user",   "content": user_msg},
        ]
        return await self._cosmos.chat_completion(messages)
