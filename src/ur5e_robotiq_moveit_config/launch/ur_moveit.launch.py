# Copyright (c) 2024 FZI Forschungszentrum Informatik
#
# Modified for: UR5e + Robotiq (URDF from ur_yt_sim) and MoveIt bringup without ur_robot_driver.

import os
import yaml
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import (
    Command,
    LaunchConfiguration,
    PathJoinSubstitution,
)
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch.substitutions import Command, LaunchConfiguration, PathJoinSubstitution, FindExecutable
from launch_ros.substitutions import FindPackageShare
from ament_index_python.packages import get_package_share_directory


PKG_MOVEIT = "ur5e_robotiq_moveit_config"
PKG_DESCRIPTION = "ur_yt_sim"


def load_yaml(package_name: str, relative_path: str):
    pkg_path = get_package_share_directory(package_name)
    abs_path = os.path.join(pkg_path, relative_path)
    with open(abs_path, "r") as f:
        return yaml.safe_load(f)


def declare_arguments():
    return [
        DeclareLaunchArgument("launch_rviz", default_value="true", description="Launch RViz?"),
        DeclareLaunchArgument(
            "ur_type",
            default_value="ur5e",
            description="UR robot type",
            choices=[
                "ur3", "ur5", "ur10",
                "ur3e", "ur5e", "ur7e", "ur10e", "ur12e", "ur16e",
                "ur8long", "ur15", "ur18", "ur20", "ur30",
            ],
        ),
        DeclareLaunchArgument(
            "warehouse_sqlite_path",
            default_value=os.path.expanduser("~/.ros/warehouse_ros.sqlite"),
            description="Path where the warehouse database should be stored",
        ),
        DeclareLaunchArgument("launch_servo", default_value="false", description="Launch Servo?"),
        DeclareLaunchArgument("use_sim_time", default_value="false", description="Use simulation time"),
    ]


def generate_launch_description():
    launch_rviz = LaunchConfiguration("launch_rviz")
    ur_type = LaunchConfiguration("ur_type")
    warehouse_sqlite_path = LaunchConfiguration("warehouse_sqlite_path")
    launch_servo = LaunchConfiguration("launch_servo")
    use_sim_time = LaunchConfiguration("use_sim_time")

    # --- Robot description from YOUR xacro (UR5e + Robotiq) ---
    robot_description_content = Command([
        FindExecutable(name="xacro"),
        " ",
        PathJoinSubstitution([FindPackageShare(PKG_DESCRIPTION), "urdf", "ur_moveit.urdf.xacro"]),
        " ",
        "ur_type:=",
        ur_type,
    ])
    robot_description = {"robot_description": robot_description_content}

    # --- SRDF (semantic) from YOUR package ---
    srdf_path = PathJoinSubstitution([FindPackageShare(PKG_MOVEIT), "srdf", "ur5e_robotiq.srdf"])
    robot_description_semantic = {
        "robot_description_semantic": ParameterValue(
            Command(["cat ", srdf_path]),
            value_type=str,
        )
    }

    # --- MoveIt YAML configs from YOUR package ---
    robot_description_kinematics = {
        "robot_description_kinematics": load_yaml(PKG_MOVEIT, "config/kinematics.yaml")
    }
    robot_description_planning = {
        "robot_description_planning": load_yaml(PKG_MOVEIT, "config/joint_limits.yaml")
    }

    # Planning pipelines (OMPL etc.)
    ompl_yaml = load_yaml(PKG_MOVEIT, "config/ompl_planning.yaml")
    planning_pipelines = {
        "planning_pipelines": ["ompl"],
        "default_planning_pipeline": "ompl",
        "ompl": ompl_yaml,
    }

    # Controllers (MoveIt Simple Controller Manager)
    moveit_controllers_yaml = load_yaml(PKG_MOVEIT, "config/moveit_controllers.yaml")
    moveit_controller_manager = {
        "moveit_controller_manager": "moveit_simple_controller_manager/MoveItSimpleControllerManager",
        **(moveit_controllers_yaml if moveit_controllers_yaml else {}),
    }

    warehouse_ros_config = {
        "warehouse_plugin": "warehouse_ros_sqlite::DatabaseConnection",
        "warehouse_host": warehouse_sqlite_path,
    }

    # --- move_group ---
    move_group_node = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=[
            robot_description,
            robot_description_semantic,
            robot_description_kinematics,
            robot_description_planning,
            planning_pipelines,
            moveit_controller_manager,
            warehouse_ros_config,
            {"use_sim_time": use_sim_time},
        ],
    )

    # --- RViz (MoveIt RViz config already exists in config/moveit.rviz) ---
    rviz_config_file = PathJoinSubstitution(
        [FindPackageShare(PKG_MOVEIT), "config", "moveit.rviz"]
    )
    rviz_node = Node(
        package="rviz2",
        condition=IfCondition(launch_rviz),
        executable="rviz2",
        name="rviz2_moveit",
        output="log",
        arguments=["-d", rviz_config_file],
        parameters=[
            robot_description,
            robot_description_semantic,
            robot_description_kinematics,
            robot_description_planning,
            planning_pipelines,
            warehouse_ros_config,
            {"use_sim_time": use_sim_time},
        ],
    )

    # --- robot_state_publisher (so RViz has TF even before MoveIt publishes state) ---
    rsp_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[robot_description, {"use_sim_time": use_sim_time}],
    )

    # --- joint_state_publisher_gui (handy for quick testing; you can remove later) ---
    jsp_gui_node = Node(
        package="joint_state_publisher_gui",
        executable="joint_state_publisher_gui",
        output="screen",
    )

    # --- Servo (optional) ---
    # NOTE: Servo needs a proper joint_state stream and often a controller setup.
    servo_node = Node(
        package="moveit_servo",
        condition=IfCondition(launch_servo),
        executable="servo_node",
        output="screen",
        parameters=[
            robot_description,
            robot_description_semantic,
            robot_description_kinematics,
            robot_description_planning,
            planning_pipelines,
            {"moveit_servo": load_yaml(PKG_MOVEIT, "config/ur_servo.yaml")},
            {"use_sim_time": use_sim_time},
        ],
    )

    return LaunchDescription(
        declare_arguments()
        + [
            rsp_node,
            jsp_gui_node,
            move_group_node,
            rviz_node,
            servo_node,
        ]
    )
