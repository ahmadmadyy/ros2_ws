from .base_skill import BaseSkill, SkillResult


HOME_JOINT_VALUES = [0.0, -1.5707, 0.0, 0.0, 0.0, 0.0]


class HomeSkill(BaseSkill):
    name = "home"
    description = "Move the arm to the home position."

    def execute(self, params: dict) -> SkillResult:
        ok = self.node.moveit_client.plan_and_execute_joints(HOME_JOINT_VALUES)
        return SkillResult(
            success=ok,
            message="Moved to home position" if ok else "Failed to reach home position",
        )

    def get_param_schema(self) -> dict:
        return {"type": "object", "properties": {}}
