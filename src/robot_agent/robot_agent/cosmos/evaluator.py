import json
import os
import re
from pathlib import Path

from ..recorder.execution_trace import ExecutionTrace
from .ollama_client import OllamaClient


# Workspace root: override with ROBOT_WS env variable, default ~/ros2_ws
_WS_ROOT = Path(os.environ.get("ROBOT_WS", Path.home() / "ros2_ws"))
_EVAL_PROMPT_FILE = _WS_ROOT / "prompts" / "eval_prompt.txt"

# Weights must sum to 1.0
_WEIGHTS = {
    "goal_achievement":    0.25,
    "consistency":         0.20,
    "plan_coherence":      0.15,
    "corrective_feedback": 0.15,
    "safety_awareness":    0.10,
    "anomaly_detection":   0.10,
    "spatial_awareness":   0.05,
}


class EvaluationEngine:
    """
    Judges Cosmos Reason2's trajectory analysis using a local Llama model.

    Pipeline (Stage 2 of 2):
        cosmos_analysis + eval_prompt.txt + trajectory JSON → Llama → structured scores
    """

    def __init__(self, ollama_client: OllamaClient):
        self._ollama = ollama_client

    async def evaluate(
        self,
        trace: ExecutionTrace,
        cosmos_reasoning_text: str,
        # task context fields (forwarded to judge prompt)
        task_description: str = "",
        object_name: str = "screwdriver",
        pick_position: str = "[0.45, 0.00, 0.09]",
        place_position: str = "N/A",
        robot_description: str = "UR5e 6-DOF manipulator with Robotiq 2F-85 gripper",
        # screwdriver-specific defaults (used for context in judge prompt)
        grasp_z: float = 0.26,
        grasp_gripper_rad: float = 0.57,
        max_trajectory_rows: int = 15,
    ) -> dict:
        """
        Stage 2: Load eval_prompt.txt, inject Cosmos output + trajectory JSON +
        task context, send to Llama for evaluation.
        Returns a dict with per-dimension scores + overall_score.
        """
        instruction = task_description or trace.label

        trajectory_json = json.dumps(
            trace.to_trajectory_json(max_rows=max_trajectory_rows), indent=2
        )

        # Load the eval_prompt.txt template and fill in all fields
        prompt_template = _EVAL_PROMPT_FILE.read_text()

        prompt = (
            prompt_template
            .replace("{task_description}", instruction)
            .replace("{object_name}", object_name)
            .replace("{pick_position}", pick_position)
            .replace("{place_position}", place_position)
            .replace("{robot_description}", robot_description)
            .replace("{cosmos_reasoning_text}", cosmos_reasoning_text or "(no Cosmos analysis provided)")
            .replace("{joint_trajectory_json}", trajectory_json)
        )

        raw = await self._ollama.chat(
            prompt,
            system="",
            json_format=True,
            temperature=0.1,
        )

        result = self._parse_response(raw)
        result["overall_score"] = self._compute_weighted_score(result)
        result["trace_id"]    = trace.trace_id
        result["trace_label"] = trace.label
        return result

    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> dict:
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {
            "parse_error":    "Could not parse Llama response as JSON",
            "raw_response":   raw,
            "overall_score":  0.0,
            "task_outcome":   "uncertain",
            "critical_failures": ["Evaluation output was not valid JSON"],
            "suggestions":    [],
        }

    def _compute_weighted_score(self, result: dict) -> float:
        total = 0.0
        for key, weight in _WEIGHTS.items():
            dim = result.get(key, {})
            score = dim.get("score", 0) if isinstance(dim, dict) else 0
            total += score * weight
        return round(total, 3)
