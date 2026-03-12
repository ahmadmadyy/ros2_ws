import json
import os
import re
from pathlib import Path

from ..recorder.execution_trace import ExecutionTrace
from .ollama_client import OllamaClient


# Workspace root: override with ROBOT_WS env variable, default ~/ros2_ws
_WS_ROOT = Path(os.environ.get("ROBOT_WS", Path.home() / "ros2_ws"))
_PROMPTS_DIR = _WS_ROOT / "prompts"
_DEFAULT_EVAL_PROMPT = "eval_screwing_rubric.txt"

# Weights must sum to 1.0
# Keys match the 5-dimension screwing rubric output (eval_screwing_rubric.txt)
_WEIGHTS = {
    "task_success":        0.30,
    "phase_correctness":   0.20,
    "screwing_quality":    0.25,
    "anomaly_severity":    0.15,
    "corrective_guidance": 0.10,
}


class EvaluationEngine:
    """
    Judges Cosmos Reason2's trajectory analysis using a local Llama model.

    Pipeline (Stage 2 of 2):
        cosmos_analysis + eval prompt + trajectory JSON -> Llama -> structured scores
    """

    def __init__(self, ollama_client: OllamaClient, prompt_file: str = _DEFAULT_EVAL_PROMPT):
        self._ollama = ollama_client
        self.prompt_file = prompt_file  # filename within prompts/

    @property
    def prompt_path(self) -> Path:
        return _PROMPTS_DIR / self.prompt_file

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
        grasp_quaternion: str = "1.0, 0.0, 0.0, 0.0",
        screw_position: str = "[0.40, 0.10, 0.00]",
        n_cycles: int = 5,
        wrist3_safe_min: float = -5.983,
        wrist3_safe_max: float = 5.983,
        screw_step_rad: float = -0.785,
        reference_analysis: str = "",
        max_trajectory_rows: int = 40,
        prompt_file: str = None,
    ) -> dict:
        """
        Stage 2: Load eval prompt, inject Cosmos output + trajectory JSON +
        task context, send to Llama for evaluation.
        Returns a dict with per-dimension scores + overall_score.

        prompt_file: override the instance-level prompt for this call only.
        """
        active_prompt = _PROMPTS_DIR / (prompt_file or self.prompt_file)

        instruction = task_description or trace.label

        trajectory_json = json.dumps(
            trace.to_trajectory_json(max_rows=max_trajectory_rows), indent=2
        )

        prompt_template = active_prompt.read_text()

        prompt = (
            prompt_template
            .replace("{task_description}", instruction)
            .replace("{object_name}", object_name)
            .replace("{pick_position}", pick_position)
            .replace("{place_position}", place_position)
            .replace("{robot_description}", robot_description)
            .replace("{cosmos_reasoning_text}", cosmos_reasoning_text or "(no Cosmos analysis provided)")
            .replace("{joint_trajectory_json}", trajectory_json)
            # screwing-specific substitutions (checklist / pairwise / temporal prompts)
            .replace("{screwdriver_position}", pick_position)
            .replace("{screw_position}", screw_position)
            .replace("{grasp_z}", str(grasp_z))
            .replace("{grasp_gripper_position}", str(grasp_gripper_rad))
            .replace("{grasp_quaternion}", grasp_quaternion)
            .replace("{n_cycles}", str(n_cycles))
            .replace("{wrist3_safe_min}", str(wrist3_safe_min))
            .replace("{wrist3_safe_max}", str(wrist3_safe_max))
            .replace("{screw_step_rad}", str(screw_step_rad))
            .replace("{reference_analysis_text}", reference_analysis or "(no reference provided)")
        )

        raw = await self._ollama.chat(
            prompt,
            system="",
            json_format=True,
            temperature=0.1,
        )

        result = self._parse_response(raw)
        result["overall_score"] = self._compute_weighted_score(result)

        # Compute task_outcome if not already set by the eval prompt
        if "task_outcome" not in result:
            s = result["overall_score"]
            result["task_outcome"] = (
                "success" if s >= 0.70 else ("partial" if s >= 0.50 else "failure")
            )

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
        # --- Pairwise format: checked FIRST to avoid key collision with rubric ---
        # (pairwise also has "corrective_guidance" key but as {verdict: ...} not {score: ...})
        if "win_count" in result:
            wc = result["win_count"]
            if isinstance(wc, dict):
                cosmos = wc.get("cosmos", 0)
                total = cosmos + wc.get("reference", 0) + wc.get("tie", 0)
                return round(cosmos / total, 3) if total else 0.0

        # --- Checklist format (trajectory_checks / reasoning_checks) ---
        if "trajectory_checks" in result or "reasoning_checks" in result:
            checks = {}
            checks.update(result.get("trajectory_checks", {}))
            checks.update(result.get("reasoning_checks", {}))
            if checks:
                passes = sum(
                    1 for c in checks.values()
                    if isinstance(c, dict) and c.get("result", "").upper() == "PASS"
                )
                return round(passes / len(checks), 3)
            return 0.0

        # --- Temporal format (overall_temporal_score) ---
        if "overall_temporal_score" in result:
            val = result["overall_temporal_score"]
            try:
                return round(float(val), 3)
            except (TypeError, ValueError):
                return 0.0

        # --- Rubric format: require "score" sub-key in at least 3 of 5 dimensions ---
        rubric_matches = sum(
            1 for k in _WEIGHTS
            if isinstance(result.get(k), dict) and "score" in result[k]
        )
        if rubric_matches >= 3:
            total = 0.0
            for key, weight in _WEIGHTS.items():
                dim = result.get(key, {})
                score = dim.get("score", 0) if isinstance(dim, dict) else 0
                total += score * weight
            return round(total / 5.0, 3)  # normalise scores 1-5 -> 0.0-1.0

        return 0.0
