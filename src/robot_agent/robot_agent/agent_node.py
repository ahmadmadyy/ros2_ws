from typing import Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState

from .moveit_client import MoveItClient
from .gripper_client import GripperClient
from .recorder.joint_recorder import JointStateRecorder
from .cosmos.cosmos_client import CosmosClient
from .cosmos.explainability import ExplainabilityEngine
from .cosmos.ollama_client import OllamaClient
from .cosmos.evaluator import EvaluationEngine
from .planner.task_planner import CosmosTaskPlanner
from .skills.base_skill import BaseSkill
from .skills.move_to_joints import MoveToJointsSkill
from .skills.move_to_pose import MoveToPoseSkill
from .skills.home import HomeSkill
from .skills.gripper_skill import GripperSkill
from .skills.pick import PickSkill
from .skills.place import PlaceSkill


class AgentNode(Node):
    """Central ROS2 node that runs inside the FastAPI process."""

    def __init__(self):
        super().__init__('robot_agent')

        # Parameters
        self.declare_parameter('cosmos_url', 'http://localhost:8000')
        self.declare_parameter('cosmos_model', 'nvidia/Cosmos-Reason2-2B')
        self.declare_parameter('ollama_url', 'http://localhost:11434')
        self.declare_parameter('ollama_model', 'llama3:latest')

        cosmos_url = self.get_parameter('cosmos_url').get_parameter_value().string_value
        cosmos_model = self.get_parameter('cosmos_model').get_parameter_value().string_value
        ollama_url = self.get_parameter('ollama_url').get_parameter_value().string_value
        ollama_model = self.get_parameter('ollama_model').get_parameter_value().string_value

        # Joint state subscriber
        self._latest_joint_state: Optional[JointState] = None
        self._joint_state_sub = self.create_subscription(
            JointState, '/joint_states', self._on_joint_state, 10
        )

        # State recorder
        self.recorder = JointStateRecorder(self)

        # MoveIt + Gripper clients
        self.moveit_client = MoveItClient(self)
        self.gripper_client = GripperClient(self)

        # Cosmos integration
        self.cosmos = CosmosClient(base_url=cosmos_url, model=cosmos_model)
        self.explainability = ExplainabilityEngine(self.cosmos)
        self.planner = CosmosTaskPlanner(self.cosmos)

        # Ollama / Llama evaluation
        self.ollama = OllamaClient(base_url=ollama_url, model=ollama_model)
        self.evaluator = EvaluationEngine(self.ollama)

        # Skills registry
        self._skills: dict[str, BaseSkill] = {}
        self._register_skills()

        self.get_logger().info('AgentNode initialized')

    def _register_skills(self):
        skills = [
            MoveToJointsSkill(self),
            MoveToPoseSkill(self),
            HomeSkill(self),
            GripperSkill(self),
            PickSkill(self),
            PlaceSkill(self),
        ]
        for skill in skills:
            self._skills[skill.name] = skill

    def _on_joint_state(self, msg: JointState):
        self._latest_joint_state = msg
        self.recorder.on_joint_state(msg)

    def get_current_joint_state(self) -> Optional[JointState]:
        return self._latest_joint_state

    def get_skill(self, name: str) -> Optional[BaseSkill]:
        return self._skills.get(name)

    def list_skills(self) -> list:
        return [
            {
                "name": s.name,
                "description": s.description,
                "param_schema": s.get_param_schema(),
            }
            for s in self._skills.values()
        ]

    def get_current_state_dict(self) -> dict:
        """Get current robot state as a dict for the planner."""
        state = {"joint_names": [], "joint_values": [], "gripper_position": None}
        js = self._latest_joint_state
        if js is not None:
            arm_vals = self.moveit_client.get_current_joint_values(js)
            if arm_vals:
                state["joint_names"] = list(self.moveit_client.ARM_JOINTS) if hasattr(self.moveit_client, 'ARM_JOINTS') else []
                state["joint_values"] = arm_vals
            # Try to get gripper position
            name_to_pos = dict(zip(js.name, js.position))
            gripper_pos = name_to_pos.get('rq_robotiq_85_left_knuckle_joint')
            state["gripper_position"] = gripper_pos
        return state
