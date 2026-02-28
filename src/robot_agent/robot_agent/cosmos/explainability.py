import json
import os
from pathlib import Path

from ..recorder.execution_trace import ExecutionTrace
from .cosmos_client import CosmosClient


# Workspace root: override with ROBOT_WS env variable, default ~/ros2_ws
_WS_ROOT = Path(os.environ.get("ROBOT_WS", Path.home() / "ros2_ws"))
_COSMOS_PROMPT_FILE = _WS_ROOT / "prompts" / "cosmos.txt"


class ExplainabilityEngine:
    """Sends execution traces to Cosmos Reason2 for deep trajectory analysis."""

    def __init__(self, cosmos_client: CosmosClient):
        self._cosmos = cosmos_client

    # ------------------------------------------------------------------
    # Primary: full structured analysis — Stage 1 of 2-stage pipeline
    # Loads cosmos.txt prompt, injects trajectory JSON + task context,
    # sends to Cosmos Reason2.
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
        Stage 1: Load cosmos.txt prompt, inject trajectory JSON + task context,
        send to Cosmos Reason2. Returns Cosmos's full reasoning text.
        """
        trajectory_json = json.dumps(
            trace.to_trajectory_json(max_rows=max_trajectory_rows), indent=2
        )

        # Derive joint names from the trace if available
        if trace.snapshots:
            joint_names_str = ", ".join(trace.snapshots[0].joint_names)
        else:
            joint_names_str = (
                "shoulder_pan_joint, shoulder_lift_joint, elbow_joint, "
                "wrist_1_joint, wrist_2_joint, wrist_3_joint, "
                "rq_robotiq_85_left_knuckle_joint"
            )

        # Load the cosmos.txt prompt template
        prompt_template = _COSMOS_PROMPT_FILE.read_text()

        cosmos_prompt = (
            prompt_template
            .replace("{robot_description}", "UR5e 6-DOF arm with Robotiq 2F-85 gripper")
            .replace("{joint_names}", joint_names_str)
            .replace("{joint_limits}", "±6.28 rad for all arm joints; gripper 0.0–0.79 rad")
            .replace("{gripper_type}", "Robotiq 2F-85 (0.0 rad = open, 0.79 rad = closed)")
            .replace("{natural_language_task}", instruction)
            .replace("{object_name}", object_name)
            .replace("{pick_position}", f"[{pick_x:.3f}, {pick_y:.3f}, {pick_z:.3f}]")
            .replace("{pick_orientation}", "upright")
            .replace("{place_position}", place_description)
            .replace("{place_orientation}", "any")
            .replace("{scene_context}", scene_context)
            .replace("{joint_trajectory_json}", trajectory_json)
        )

        messages = [
            {"role": "user", "content": cosmos_prompt},
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
