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

    plan_node = Node(
        package="api_robot",
        executable="joint_space_plan_only",
        name="joint_space_plan_only",
        output="screen",
    )

    # Give bringup time to start move_group
    delayed_plan = TimerAction(period=6.0, actions=[plan_node])

    return LaunchDescription([
        DeclareLaunchArgument("ur_type", default_value="ur5e"),
        bringup,
        delayed_plan,
    ])
