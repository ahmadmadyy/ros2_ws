import json
import re

from ..recorder.execution_trace import ExecutionTrace
from .ollama_client import OllamaClient


# ---------------------------------------------------------------------------
# PROMPT 2 — sent to Llama (judge)
# Role: Receive Cosmos's trajectory analysis and produce structured scores.
# This prompt intentionally does NOT re-send the raw trajectory — the judge
# works from the Cosmos reasoning alone, assessing reasoning quality + outcome.
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = """\
You are an expert judge evaluating the quality of an AI reasoning model's \
analysis of a robot manipulation task. You will be given:
1. The task context (what the robot was asked to do, object location, scene).
2. The full analysis produced by Cosmos Reason2 after observing the robot's \
joint trajectory.

Your job is to judge how good that analysis is and whether it correctly \
assessed the task outcome. Score each dimension 1 (poor) to 5 (excellent)."""


_JUDGE_USER_TEMPLATE = """\
## Task Context
- **Instruction**: {instruction}
- **Object**: {object_name} at position {pick_position}
- **Place target**: {place_description}
- **Robot**: UR5e 6-DOF arm + Robotiq 2F-85 gripper
- **Expected grasp height** (tool0): {grasp_z:.3f} m
- **Expected gripper position at grasp**: {grasp_gripper_rad:.2f} rad

## Cosmos Reason2 Analysis
{cosmos_analysis}

---

## Evaluation Rubric

Score each dimension 1–5 with a one-sentence justification.

### Reasoning Quality
1. **Spatial Awareness** (1–5): Did Cosmos correctly identify object positions, \
approach/grasp heights, and spatial relationships from the trajectory?
2. **Plan Coherence** (1–5): Are the identified phases logically ordered and \
physically plausible? Are there missing phases or contradictions?
3. **Safety Awareness** (1–5): Did Cosmos flag collision risks, joint-limit issues, \
or gripper constraint violations where relevant?

### Task Outcome Assessment
4. **Goal Achievement** (1–5): Did Cosmos correctly assess whether the pick succeeded \
(gripper closed at right height + object attached + arm retreated)? \
5 = clear, evidence-backed verdict; 1 = no verdict or clearly wrong.
5. **Anomaly Detection** (1–5): Did Cosmos correctly identify stationary segments, \
sudden jumps, or timing issues in the trajectory?

### Feedback Quality
6. **Corrective Feedback** (1–5): If the task failed or was suboptimal, are the \
corrections specific, actionable, and numerically grounded? \
If the task succeeded and Cosmos correctly said so, score 5.
7. **Consistency** (1–5): Is Cosmos's stated phase segmentation consistent with \
its success verdict and its corrective feedback? No internal contradictions?

## Output Format
Return ONLY valid JSON with exactly this structure:
{{
  "spatial_awareness":   {{"score": <int 1-5>, "justification": "<str>"}},
  "plan_coherence":      {{"score": <int 1-5>, "justification": "<str>"}},
  "safety_awareness":    {{"score": <int 1-5>, "justification": "<str>"}},
  "goal_achievement":    {{"score": <int 1-5>, "justification": "<str>"}},
  "anomaly_detection":   {{"score": <int 1-5>, "justification": "<str>"}},
  "corrective_feedback": {{"score": <int 1-5>, "justification": "<str>"}},
  "consistency":         {{"score": <int 1-5>, "justification": "<str>"}},
  "overall_score":       <float>,
  "task_outcome":        "<succeeded | failed | uncertain>",
  "critical_failures":   ["<list any serious errors in the Cosmos analysis>"],
  "suggestions":         ["<list ways the Cosmos analysis could be improved>"]
}}

Compute overall_score as the weighted average:
  Goal Achievement:    25%
  Consistency:         20%
  Plan Coherence:      15%
  Corrective Feedback: 15%
  Safety Awareness:    10%
  Anomaly Detection:   10%
  Spatial Awareness:    5%"""


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

    Pipeline:
        Trace → Cosmos (explainability.py) → cosmos_analysis (str)
        cosmos_analysis + task context → Llama (this class) → structured scores
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
    ) -> dict:
        """
        Send Cosmos's analysis to Llama for judging.
        Returns a dict with per-dimension scores + overall_score.
        """
        instruction = task_description or trace.label

        prompt = _JUDGE_USER_TEMPLATE.format(
            instruction=instruction,
            object_name=object_name,
            pick_position=pick_position,
            place_description=place_position,
            grasp_z=grasp_z,
            grasp_gripper_rad=grasp_gripper_rad,
            cosmos_analysis=cosmos_reasoning_text or "(no Cosmos analysis provided)",
        )

        raw = await self._ollama.chat(
            prompt,
            system=_JUDGE_SYSTEM_PROMPT,
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
