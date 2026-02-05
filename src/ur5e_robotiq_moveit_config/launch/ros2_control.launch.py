import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    use_sim_time = LaunchConfiguration("use_sim_time")

    pkg = get_package_share_directory("ur5e_robotiq_moveit_config")
    controllers_yaml = os.path.join(pkg, "config", "ros2_controllers.yaml")

    # ros2_control_node expects robot_description on the parameter server.
    # We will rely on MoveIt launch to publish robot_description, so run this together with MoveIt.
    ros2_control_node = Node(
        package="controller_manager",
        executable="ros2_control_node",
        parameters=[{"use_sim_time": use_sim_time}, controllers_yaml],
        output="screen",
    )

    joint_state_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_state_broadcaster", "--controller-manager", "/controller_manager"],
        output="screen",
    )

    arm_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["scaled_joint_trajectory_controller", "--controller-manager", "/controller_manager"],
        output="screen",
    )

    gripper_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["robotiq_gripper_controller", "--controller-manager", "/controller_manager"],
        output="screen",
    )

    ld = LaunchDescription()
    ld.add_action(DeclareLaunchArgument("use_sim_time", default_value="false"))
    ld.add_action(ros2_control_node)
    ld.add_action(joint_state_spawner)
    ld.add_action(arm_spawner)
    ld.add_action(gripper_spawner)
    return ld
