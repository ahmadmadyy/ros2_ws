from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SkillResult:
    success: bool
    message: str
    trace_id: Optional[str] = None


class BaseSkill(ABC):
    """Abstract base class for robot skills."""

    name: str = ""
    description: str = ""

    def __init__(self, node):
        self.node = node

    @abstractmethod
    def execute(self, params: dict) -> SkillResult:
        """Execute the skill with the given parameters."""
        ...

    @abstractmethod
    def get_param_schema(self) -> dict:
        """Return JSON schema describing expected params."""
        ...

    def execute_with_recording(self, params: dict) -> SkillResult:
        """Execute skill while recording joint states."""
        trace_id = self.node.recorder.start_recording(label=self.name)
        result = self.execute(params)
        self.node.recorder.stop_recording()
        result.trace_id = trace_id
        return result
