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

    plan_exec_node = Node(
        package="api_robot",
        executable="joint_space_plan_exec",
        name="joint_space_plan_exec",
        output="screen",
        parameters=[{
            "arm_group":    "ur_manipulator",
            "planning_time": 10.0,
            "num_attempts":  10,

            # 6 joint angles [rad]:
            #   [shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3]
            #
            # Named SRDF states for reference:
            #   home : [0.0, -1.5708,  0.0,      0.0,     0.0, 0.0]
            #   up   : [0.0, -1.5708,  0.0,     -1.5708,  0.0, 0.0]
            "arm_joint_target": [0.0, -2.3562, 1.5708, -1.5708, -1.5708, 0.0],
        }],
    )

    delayed_node = TimerAction(period=6.0, actions=[plan_exec_node])

    return LaunchDescription([
        DeclareLaunchArgument("ur_type", default_value="ur5e"),
        bringup,
        delayed_node,
    ])
