import json
import re
from typing import List, Optional

from ..cosmos.cosmos_client import CosmosClient
from .prompt_templates import build_planner_messages


class CosmosTaskPlanner:
    """Uses Cosmos Reason2 to decompose natural language instructions into skill sequences."""

    def __init__(self, cosmos_client: CosmosClient):
        self._cosmos = cosmos_client

    async def plan(
        self,
        instruction: str,
        available_skills: List[dict],
        current_state: dict,
    ) -> dict:
        """
        Send instruction + skill catalog to Cosmos, parse JSON plan.
        Returns {"plan": [...], "reasoning": "..."}.
        """
        messages = build_planner_messages(instruction, available_skills, current_state)

        raw_response = await self._cosmos.chat_completion(
            messages, temperature=0.3, max_tokens=2048
        )

        plan = self._parse_plan(raw_response)
        return {
            "plan": plan,
            "reasoning": raw_response,
        }

    def _parse_plan(self, response: str) -> List[dict]:
        """Extract JSON array from LLM response."""
        # Try direct JSON parse first
        try:
            parsed = json.loads(response.strip())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        # Try to find JSON array in the response
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass

        return []
