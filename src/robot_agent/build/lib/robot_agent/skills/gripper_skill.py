from .base_skill import BaseSkill, SkillResult


class GripperSkill(BaseSkill):
    name = "gripper"
    description = "Open or close the Robotiq 2F-85 gripper."

    def execute(self, params: dict) -> SkillResult:
        action = params.get("action", "open")

        if action == "open":
            ok = self.node.gripper_client.open()
        elif action == "close":
            ok = self.node.gripper_client.close()
        elif action == "set":
            position = float(params.get("position", 0.0))
            ok = self.node.gripper_client.set_position(position)
        else:
            return SkillResult(success=False, message=f"Unknown gripper action: {action}")

        return SkillResult(
            success=ok,
            message=f"Gripper {action} succeeded" if ok else f"Gripper {action} failed",
        )

    def get_param_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "close", "set"],
                    "description": "Gripper action to perform",
                },
                "position": {
                    "type": "number",
                    "description": "Target position for 'set' action (0.0=open, 0.79=closed)",
                },
            },
            "required": ["action"],
        }
