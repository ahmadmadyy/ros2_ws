import os
import yaml

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

from ament_index_python.packages import get_package_share_directory
from moveit_configs_utils import MoveItConfigsBuilder


PKG_MOVEIT_CONFIG = "ur5e_robotiq_moveit_config"


def load_yaml(package_name: str, rel_path: str):
    pkg_path = get_package_share_directory(package_name)
    abs_path = os.path.join(pkg_path, rel_path)
    try:
        with open(abs_path, "r") as f:
            return yaml.safe_load(f)
    except OSError:
        return None


def generate_launch_description():
    # ---------------- Launch args ----------------
    launch_rviz = LaunchConfiguration("launch_rviz")
    ur_type = LaunchConfiguration("ur_type")
    warehouse_sqlite_path = LaunchConfiguration("warehouse_sqlite_path")
    launch_servo = LaunchConfiguration("launch_servo")
    use_sim_time = LaunchConfiguration("use_sim_time")
    publish_robot_description_semantic = LaunchConfiguration("publish_robot_description_semantic")

    # ---------------- MoveIt config ----------------
    # IMPORTANT:
    # MoveItConfigsBuilder expects file_path to be a STRING (or pathlib.Path),
    # relative to the package share directory. Do NOT pass PathJoinSubstitution here.
    moveit_config = (
        MoveItConfigsBuilder(robot_name="ur", package_name=PKG_MOVEIT_CONFIG)
        .robot_description(
            file_path="urdf/ur_moveit.urdf.xacro",
            mappings={"ur_type": ur_type},
        )
        .robot_description_semantic(
            file_path="srdf/ur.srdf.xacro",
            mappings={"name": ur_type},
        )
        .to_moveit_configs()
    )

    warehouse_ros_config = {
        "warehouse_plugin": "warehouse_ros_sqlite::DatabaseConnection",
        "warehouse_host": warehouse_sqlite_path,
    }

    # ---------------- Nodes ----------------
    move_group_node = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=[
            moveit_config.to_dict(),
            warehouse_ros_config,
            {
                "use_sim_time": use_sim_time,
                "publish_robot_description_semantic": publish_robot_description_semantic,
            },
        ],
    )

    servo_yaml = load_yaml(PKG_MOVEIT_CONFIG, "config/ur_servo.yaml")
    servo_node = Node(
        package="moveit_servo",
        condition=IfCondition(launch_servo),
        executable="servo_node",
        output="screen",
        parameters=[
            moveit_config.to_dict(),
            {"moveit_servo": servo_yaml} if servo_yaml else {},
        ],
    )

    rviz_config_file = os.path.join(
        get_package_share_directory(PKG_MOVEIT_CONFIG),
        "config",
        "moveit.rviz",
    )
    rviz_node = Node(
        package="rviz2",
        condition=IfCondition(launch_rviz),
        executable="rviz2",
        name="rviz2_moveit",
        output="log",
        arguments=["-d", rviz_config_file],
        parameters=[
            moveit_config.robot_description,
            moveit_config.robot_description_semantic,
            moveit_config.robot_description_kinematics,
            moveit_config.planning_pipelines,
            moveit_config.joint_limits,
            warehouse_ros_config,
            {"use_sim_time": use_sim_time},
        ],
    )

    # ---------------- LaunchDescription ----------------
    ld = LaunchDescription()

    ld.add_action(DeclareLaunchArgument("launch_rviz", default_value="true"))
    ld.add_action(
        DeclareLaunchArgument(
            "ur_type",
            default_value="ur5e",
            choices=[
                "ur3", "ur5", "ur10",
                "ur3e", "ur5e", "ur7e", "ur10e", "ur12e", "ur16e",
                "ur8long", "ur15", "ur18", "ur20", "ur30",
            ],
        )
    )
    ld.add_action(
        DeclareLaunchArgument(
            "warehouse_sqlite_path",
            default_value=os.path.expanduser("~/.ros/warehouse_ros.sqlite"),
        )
    )
    ld.add_action(DeclareLaunchArgument("launch_servo", default_value="false"))
    ld.add_action(DeclareLaunchArgument("use_sim_time", default_value="false"))
    ld.add_action(DeclareLaunchArgument("publish_robot_description_semantic", default_value="true"))

    ld.add_action(move_group_node)
    ld.add_action(rviz_node)
    ld.add_action(servo_node)

    return ld
