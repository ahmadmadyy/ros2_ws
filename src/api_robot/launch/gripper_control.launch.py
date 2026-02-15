from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    ur_type = LaunchConfiguration("ur_type")

    bringup_pkg = get_package_share_directory("ur5e_robotiq_moveit_config")
    bringup_launch = os.path.join(bringup_pkg, "launch", "bringup.launch.py")

    bringup = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(bringup_launch),
        launch_arguments={"ur_type": ur_type}.items(),
    )

    gripper_node = Node(
        package="api_robot",
        executable="gripper_control",
        name="gripper_control",
        output="screen",
        parameters=[{
            "gripper_action": "/gripper_controller/gripper_cmd",
            # Robotiq 2F-85: 0.0 = fully open, ~0.78 = fully closed
            "gripper_open":  0.0,
            "gripper_close": 0.78,
            "max_effort":    0.0,
        }],
    )

    delayed_node = TimerAction(period=6.0, actions=[gripper_node])

    return LaunchDescription([
        DeclareLaunchArgument("ur_type", default_value="ur5e"),
        bringup,
        delayed_node,
    ])
