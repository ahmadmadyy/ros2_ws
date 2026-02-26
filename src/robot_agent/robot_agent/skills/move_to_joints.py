from .base_skill import BaseSkill, SkillResult


class MoveToJointsSkill(BaseSkill):
    name = "move_to_joints"
    description = "Move the arm to specific joint values (6 floats in radians)."

    def execute(self, params: dict) -> SkillResult:
        joint_values = params.get("joint_values")
        if not joint_values or len(joint_values) != 6:
            return SkillResult(success=False, message="joint_values must be a list of 6 floats")

        ok = self.node.moveit_client.plan_and_execute_joints(joint_values)
        return SkillResult(
            success=ok,
            message="Moved to joint target" if ok else "Failed to plan or execute joint goal",
        )

    def get_param_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "joint_values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 6,
                    "maxItems": 6,
                    "description": "6 joint angles in radians [shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3]",
                }
            },
            "required": ["joint_values"],
        }
