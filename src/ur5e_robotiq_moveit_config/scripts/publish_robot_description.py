#!/usr/bin/env python3
import subprocess
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
from std_msgs.msg import String


class RobotDescriptionPublisher(Node):
    def __init__(self):
        super().__init__("robot_description_topic_publisher")

        self.declare_parameter("xacro_path", "")
        self.declare_parameter("ur_type", "ur5e")
        self.declare_parameter("use_fake_hardware", True)

        xacro_path = self.get_parameter("xacro_path").get_parameter_value().string_value
        ur_type = self.get_parameter("ur_type").get_parameter_value().string_value
        use_fake = self.get_parameter("use_fake_hardware").get_parameter_value().bool_value

        if not xacro_path:
            raise RuntimeError("Parameter 'xacro_path' is empty")

        cmd = [
            "xacro",
            xacro_path,
            f"ur_type:={ur_type}",
            f"use_fake_hardware:={'true' if use_fake else 'false'}",
        ]
        self.get_logger().info("Generating robot_description via xacro...")
        urdf = subprocess.check_output(cmd, text=True)

        qos = QoSProfile(depth=1)
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        qos.reliability = ReliabilityPolicy.RELIABLE

        self.pub = self.create_publisher(String, "/robot_description", qos)

        msg = String()
        msg.data = urdf

        # publish repeatedly for a few seconds so late subscribers get it
        self.get_logger().info("Publishing /robot_description (transient_local)...")
        end = time.time() + 5.0
        while time.time() < end and rclpy.ok():
            self.pub.publish(msg)
            rclpy.spin_once(self, timeout_sec=0.1)
            time.sleep(0.2)

        self.get_logger().info("Done publishing /robot_description. Staying alive.")
        # keep node alive so it's visible; transient_local already latches
        self.timer = self.create_timer(10.0, lambda: None)


def main():
    rclpy.init()
    node = RobotDescriptionPublisher()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
