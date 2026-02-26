#!/usr/bin/env python3

from typing import Dict, List

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState


class JointStateToIsaacBridge(Node):
    def __init__(self) -> None:
        super().__init__("joint_state_to_isaac_bridge")

        self.declare_parameter("source_topic", "/joint_states")
        self.declare_parameter("command_topic", "/isaac_joint_commands")
        self.declare_parameter(
            "joint_names",
            [
                "shoulder_pan_joint",
                "shoulder_lift_joint",
                "elbow_joint",
                "wrist_1_joint",
                "wrist_2_joint",
                "wrist_3_joint",
                "rq_robotiq_85_left_knuckle_joint",
            ],
        )

        source_topic = self.get_parameter("source_topic").get_parameter_value().string_value
        command_topic = self.get_parameter("command_topic").get_parameter_value().string_value
        self.joint_names = list(
            self.get_parameter("joint_names").get_parameter_value().string_array_value
        )

        self.publisher = self.create_publisher(JointState, command_topic, 20)
        self.subscription = self.create_subscription(
            JointState, source_topic, self._on_joint_state, 20
        )

        self.get_logger().info(
            f"Bridging {source_topic} -> {command_topic} for joints: {self.joint_names}"
        )

    def _on_joint_state(self, msg: JointState) -> None:
        if not msg.name or not msg.position:
            return

        index_by_name: Dict[str, int] = {name: i for i, name in enumerate(msg.name)}

        out = JointState()
        out.header = msg.header
        out.name = []
        out.position = []
        out.velocity = []
        out.effort = []

        for joint_name in self.joint_names:
            i = index_by_name.get(joint_name)
            if i is None:
                continue

            out.name.append(joint_name)
            out.position.append(msg.position[i])

            if len(msg.velocity) > i:
                out.velocity.append(msg.velocity[i])
            if len(msg.effort) > i:
                out.effort.append(msg.effort[i])

        if not out.name:
            return

        self.publisher.publish(out)


def main() -> None:
    rclpy.init()
    node = JointStateToIsaacBridge()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
