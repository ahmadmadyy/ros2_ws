from geometry_msgs.msg import Pose

from .base_skill import BaseSkill, SkillResult


class MoveToPoseSkill(BaseSkill):
    name = "move_to_pose"
    description = "Move the end effector to a Cartesian pose (position + orientation)."

    def execute(self, params: dict) -> SkillResult:
        pose_data = params.get("pose")
        if not pose_data:
            return SkillResult(success=False, message="pose is required")

        pose = Pose()
        pose.position.x = float(pose_data.get("x", 0.0))
        pose.position.y = float(pose_data.get("y", 0.0))
        pose.position.z = float(pose_data.get("z", 0.0))
        pose.orientation.x = float(pose_data.get("qx", 0.0))
        pose.orientation.y = float(pose_data.get("qy", 0.0))
        pose.orientation.z = float(pose_data.get("qz", 0.0))
        pose.orientation.w = float(pose_data.get("qw", 1.0))

        ok = self.node.moveit_client.plan_and_execute_pose(pose)
        return SkillResult(
            success=ok,
            message="Moved to pose target" if ok else "Failed to plan or execute pose goal",
        )

    def get_param_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pose": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "z": {"type": "number"},
                        "qx": {"type": "number", "default": 0.0},
                        "qy": {"type": "number", "default": 0.0},
                        "qz": {"type": "number", "default": 0.0},
                        "qw": {"type": "number", "default": 1.0},
                    },
                    "required": ["x", "y", "z"],
                }
            },
            "required": ["pose"],
        }
