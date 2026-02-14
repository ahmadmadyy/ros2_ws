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

    demo_node = Node(
        package="api_robot",
        executable="joint_space_demo",
        name="api_robot_joint_space_demo",
        output="screen",
        parameters=[{
            # MoveIt arm group (exists in your SRDF)
            "arm_group": "ur_manipulator",

            # ros2_control gripper action + joint (exists in controller)
            "gripper_action": "/robotiq_gripper_controller/follow_joint_trajectory",
            "gripper_joint": "rq_robotiq_85_left_knuckle_joint",

            "gripper_open": 0.0,
            "gripper_close": 0.79,

            "arm_joint_target": [0.0, -2.3562, 1.5708, -1.5708, -1.5708, 0.0],
        }],
    )

    delayed_demo = TimerAction(period=6.0, actions=[demo_node])

    return LaunchDescription([
        DeclareLaunchArgument("ur_type", default_value="ur5e"),
        bringup,
        delayed_demo,
    ])
