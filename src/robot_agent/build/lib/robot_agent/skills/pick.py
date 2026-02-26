import math

from geometry_msgs.msg import Pose

from .base_skill import BaseSkill, SkillResult


# Canonical home joints (matches test_pick_screwdriver.py)
_HOME_JOINTS = [0.0, -math.pi / 2, 0.0, -math.pi / 2, 0.0, 0.0]

# A pre-computed "forward approach" seed for picking at ~x=0.45, y=0 from above.
# shoulder_pan=0 keeps the arm pointing forward; the remaining joints place
# tool0 roughly above the screwdriver.  The IK solver uses this as a warm-start
# to stay in the front/elbow-up kinematic branch rather than the "back" branch
# (shoulder_pan≈−2.84) that OMPL then cannot plan to from home.
_APPROACH_SEED = [0.0, -2.2, 1.9, -1.28, -1.571, 0.0]

# Robotiq 2F-85 gripper links used as touch_links when attaching an object
_GRIPPER_LINKS = [
    'tool0',
    'rq_robotiq_85_base_link',
    'rq_robotiq_85_left_knuckle_link',
    'rq_robotiq_85_right_knuckle_link',
    'rq_robotiq_85_left_finger_link',
    'rq_robotiq_85_right_finger_link',
    'rq_robotiq_85_left_inner_knuckle_link',
    'rq_robotiq_85_right_inner_knuckle_link',
    'rq_robotiq_85_left_finger_tip_link',
    'rq_robotiq_85_right_finger_tip_link',
]


def _make_pose(x, y, z, qx, qy, qz, qw) -> Pose:
    p = Pose()
    p.position.x = x
    p.position.y = y
    p.position.z = z
    p.orientation.x = qx
    p.orientation.y = qy
    p.orientation.z = qz
    p.orientation.w = qw
    return p


def _ik_and_move(
    moveit,
    pose: Pose,
    velocity_scaling: float = 0.3,
    seed=None,
    extra_seeds=None,
) -> bool:
    """IK → joint values → plan_and_execute_joints.

    Tries seeds in order (primary seed first, then extra_seeds).  Returns True
    as soon as one IK+plan+execute combination succeeds.  This guards against
    the IK solver landing in a "back" kinematic branch (shoulder_pan ≈ −163°)
    that OMPL cannot plan to from the home configuration.
    """
    all_seeds = [seed] + list(extra_seeds or [])
    for s in all_seeds:
        joints = moveit.ik(pose, seed_joints=s)
        if joints is None:
            continue
        ok = moveit.plan_and_execute_joints(joints, velocity_scaling=velocity_scaling)
        if ok:
            return True
    return False


class PickSkill(BaseSkill):
    name = "pick"
    description = (
        "Full pick sequence matching test_pick_screwdriver.py: go home, open gripper, "
        "IK-based approach, allow collision, descend, close to object width, "
        "attach object, retreat upward."
    )

    def execute(self, params: dict) -> SkillResult:
        pose_data = params.get("pose")
        if not pose_data:
            return SkillResult(success=False, message="pose is required")

        # Pose parameters — default orientation is straight-down (qx=1, qw=0)
        x  = float(pose_data.get("x", 0.0))
        y  = float(pose_data.get("y", 0.0))
        z  = float(pose_data.get("z", 0.0))
        qx = float(pose_data.get("qx", 1.0))
        qy = float(pose_data.get("qy", 0.0))
        qz = float(pose_data.get("qz", 0.0))
        qw = float(pose_data.get("qw", 0.0))

        approach_height       = float(params.get("approach_height", 0.18))
        retreat_height        = float(params.get("retreat_height", 0.18))
        grasp_gripper_position = float(params.get("grasp_gripper_position", 0.57))
        object_id             = str(params.get("object_id", "screwdriver"))
        go_home_first         = bool(params.get("go_home_first", True))

        moveit  = self.node.moveit_client
        gripper = self.node.gripper_client

        grasp_pose    = _make_pose(x, y, z, qx, qy, qz, qw)
        approach_pose = _make_pose(x, y, z + approach_height, qx, qy, qz, qw)
        retreat_pose  = _make_pose(x, y, z + retreat_height,  qx, qy, qz, qw)

        steps = []

        # Step 1: go home (gives a reliable, singularity-free seed for IK)
        if go_home_first:
            steps.append((
                "Go to home joints",
                lambda: moveit.plan_and_execute_joints(_HOME_JOINTS, velocity_scaling=0.3),
            ))

        # Step 2: open gripper
        steps.append(("Open gripper", lambda: gripper.open()))

        # Step 3: IK + move to approach pose
        steps.append((
            "IK + move to approach pose",
            lambda ap=approach_pose: _ik_and_move(
                moveit, ap, velocity_scaling=0.3,
                seed=_HOME_JOINTS, extra_seeds=[_APPROACH_SEED],
            ),
        ))

        # Step 4: allow gripper↔object collision so the descent doesn't fail
        steps.append((
            f"Allow gripper↔{object_id} collision",
            lambda oid=object_id: moveit.allow_collision(oid),
        ))

        # Step 5: IK + descend to grasp pose
        steps.append((
            "IK + descend to grasp pose",
            lambda gp=grasp_pose: _ik_and_move(
                moveit, gp, velocity_scaling=0.2,
                seed=_APPROACH_SEED, extra_seeds=[_HOME_JOINTS],
            ),
        ))

        # Step 6: close gripper to object width (not fully closed)
        steps.append((
            f"Close gripper to position {grasp_gripper_position:.2f} rad",
            lambda pos=grasp_gripper_position: gripper.set_position(pos),
        ))

        # Step 7: attach object so it follows the arm during retreat
        steps.append((
            f"Attach {object_id} to gripper",
            lambda oid=object_id: moveit.attach_object(
                oid, "tool0", touch_links=_GRIPPER_LINKS
            ),
        ))

        # Step 8: IK + retreat upward
        steps.append((
            "IK + retreat upward",
            lambda rp=retreat_pose: _ik_and_move(
                moveit, rp, velocity_scaling=0.3,
                seed=_APPROACH_SEED, extra_seeds=[_HOME_JOINTS],
            ),
        ))

        for label, fn in steps:
            self.node.get_logger().info(f"[PickSkill] {label}")
            ok = fn()
            if not ok:
                return SkillResult(success=False, message=f"Failed at: {label}")

        return SkillResult(success=True, message="Pick completed successfully")

    def get_param_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pose": {
                    "type": "object",
                    "description": "Grasp pose for tool0 (straight-down orientation by default)",
                    "properties": {
                        "x":  {"type": "number"},
                        "y":  {"type": "number"},
                        "z":  {"type": "number"},
                        "qx": {"type": "number", "default": 1.0},
                        "qy": {"type": "number", "default": 0.0},
                        "qz": {"type": "number", "default": 0.0},
                        "qw": {"type": "number", "default": 0.0},
                    },
                    "required": ["x", "y", "z"],
                },
                "approach_height": {
                    "type": "number",
                    "default": 0.18,
                    "description": "Height above grasp z for approach (m)",
                },
                "retreat_height": {
                    "type": "number",
                    "default": 0.18,
                    "description": "Height above grasp z for post-grasp retreat (m)",
                },
                "grasp_gripper_position": {
                    "type": "number",
                    "default": 0.57,
                    "description": (
                        "Robotiq 2F-85 position (rad) to close to. "
                        "0.0=fully open (85 mm), 0.79=fully closed. "
                        "0.57 ≈ 24 mm gap, matching a screwdriver."
                    ),
                },
                "object_id": {
                    "type": "string",
                    "default": "screwdriver",
                    "description": "Planning-scene object ID to allow collision with and attach after grasp",
                },
                "go_home_first": {
                    "type": "boolean",
                    "default": True,
                    "description": "Move to home joints before approaching (recommended for reliable IK)",
                },
            },
            "required": ["pose"],
        }
