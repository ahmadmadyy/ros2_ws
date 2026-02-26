from copy import deepcopy

from geometry_msgs.msg import Pose

from .base_skill import BaseSkill, SkillResult


class PlaceSkill(BaseSkill):
    name = "place"
    description = "Approach a target pose from above, open the gripper to release, then retreat upward."

    def execute(self, params: dict) -> SkillResult:
        pose_data = params.get("pose")
        if not pose_data:
            return SkillResult(success=False, message="pose is required")

        approach_height = float(params.get("approach_height", 0.1))
        retreat_height = float(params.get("retreat_height", 0.1))

        target = Pose()
        target.position.x = float(pose_data.get("x", 0.0))
        target.position.y = float(pose_data.get("y", 0.0))
        target.position.z = float(pose_data.get("z", 0.0))
        target.orientation.x = float(pose_data.get("qx", 0.0))
        target.orientation.y = float(pose_data.get("qy", 0.0))
        target.orientation.z = float(pose_data.get("qz", 0.0))
        target.orientation.w = float(pose_data.get("qw", 1.0))

        # 1. Move to approach pose (above target)
        approach = deepcopy(target)
        approach.position.z += approach_height
        ok = self.node.moveit_client.plan_and_execute_pose(approach)
        if not ok:
            return SkillResult(success=False, message="Failed to reach approach pose")

        # 2. Descend to place pose
        ok = self.node.moveit_client.plan_and_execute_pose(target)
        if not ok:
            return SkillResult(success=False, message="Failed to reach place pose")

        # 3. Open gripper to release
        ok = self.node.gripper_client.open()
        if not ok:
            return SkillResult(success=False, message="Failed to open gripper")

        # 4. Retreat upward
        retreat = deepcopy(target)
        retreat.position.z += retreat_height
        ok = self.node.moveit_client.plan_and_execute_pose(retreat)
        if not ok:
            return SkillResult(success=False, message="Failed to retreat after place")

        return SkillResult(success=True, message="Place completed successfully")

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
                },
                "approach_height": {"type": "number", "default": 0.1},
                "retreat_height": {"type": "number", "default": 0.1},
            },
            "required": ["pose"],
        }
