import os
import yaml

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, RegisterEventHandler
from launch.event_handlers import OnProcessStart
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration

from launch_ros.actions import Node

from ament_index_python.packages import get_package_share_directory
from moveit_configs_utils import MoveItConfigsBuilder


PKG = "ur5e_robotiq_moveit_config"


def load_yaml(package_name: str, rel_path: str):
    pkg_path = get_package_share_directory(package_name)
    abs_path = os.path.join(pkg_path, rel_path)
    try:
        with open(abs_path, "r") as f:
            return yaml.safe_load(f)
    except OSError:
        return None


def generate_launch_description():
    ur_type = LaunchConfiguration("ur_type")
    launch_rviz = LaunchConfiguration("launch_rviz")
    use_sim_time = LaunchConfiguration("use_sim_time")
    use_fake_hardware = LaunchConfiguration("use_fake_hardware")

    warehouse_sqlite_path = LaunchConfiguration("warehouse_sqlite_path")
    launch_servo = LaunchConfiguration("launch_servo")
    publish_robot_description_semantic = LaunchConfiguration("publish_robot_description_semantic")

    pkg_share = get_package_share_directory(PKG)

    # Publish /robot_description topic (needed by controller_manager on Jazzy)
    robot_description_publisher = Node(
        package=PKG,
        executable="publish_robot_description.py",
        output="screen",
        parameters=[
            {
                "xacro_path": os.path.join(pkg_share, "urdf", "ur_moveit.urdf.xacro"),
                "ur_type": ur_type,
                "use_fake_hardware": use_fake_hardware,
            }
        ],
    )

    # ros2_control
    controllers_yaml = os.path.join(pkg_share, "config", "ros2_controllers.yaml")
    ros2_control_node = Node(
        package="controller_manager",
        executable="ros2_control_node",
        output="screen",
        parameters=[controllers_yaml, {"use_sim_time": use_sim_time}],
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
        arguments=["gripper_controller", "--controller-manager", "/controller_manager"],
        output="screen",
    )

    # Spawn controllers only after controller_manager starts
    spawn_after_cm = RegisterEventHandler(
        OnProcessStart(
            target_action=ros2_control_node,
            on_start=[joint_state_spawner, arm_spawner, gripper_spawner],
        )
    )

    # MoveIt config (use files inside this package)
    moveit_config = (
        MoveItConfigsBuilder(robot_name="ur", package_name=PKG)
        .robot_description(file_path="urdf/ur_moveit.urdf.xacro", mappings={"ur_type": ur_type, "use_fake_hardware": use_fake_hardware})
        .robot_description_semantic(file_path="srdf/ur.srdf.xacro", mappings={"name": ur_type})
        .to_moveit_configs()
    )

    warehouse_ros_config = {
        "warehouse_plugin": "warehouse_ros_sqlite::DatabaseConnection",
        "warehouse_host": warehouse_sqlite_path,
    }

    # Publish TF from URDF + /joint_states
    robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[
            moveit_config.robot_description,
            {"use_sim_time": use_sim_time},
        ],
    )

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

    servo_yaml = load_yaml(PKG, "config/ur_servo.yaml")
    servo_node = Node(
        package="moveit_servo",
        condition=IfCondition(launch_servo),
        executable="servo_node",
        output="screen",
        parameters=[moveit_config.to_dict(), {"moveit_servo": servo_yaml} if servo_yaml else {}],
    )

    rviz_config_file = os.path.join(pkg_share, "config", "moveit.rviz")
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

    ld = LaunchDescription()
    ld.add_action(DeclareLaunchArgument("ur_type", default_value="ur5e"))
    ld.add_action(DeclareLaunchArgument("launch_rviz", default_value="true"))
    ld.add_action(DeclareLaunchArgument("use_sim_time", default_value="false"))
    ld.add_action(DeclareLaunchArgument("use_fake_hardware", default_value="true"))
    ld.add_action(DeclareLaunchArgument("launch_servo", default_value="false"))
    ld.add_action(DeclareLaunchArgument("publish_robot_description_semantic", default_value="true"))
    ld.add_action(
        DeclareLaunchArgument(
            "warehouse_sqlite_path",
            default_value=os.path.expanduser("~/.ros/warehouse_ros.sqlite"),
        )
    )

    # Order matters: publish /robot_description first, then robot_state_publisher (TF),
    # then controller_manager, then MoveIt
    ld.add_action(robot_description_publisher)
    ld.add_action(robot_state_publisher)
    ld.add_action(ros2_control_node)
    ld.add_action(spawn_after_cm)
    ld.add_action(move_group_node)
    ld.add_action(rviz_node)
    ld.add_action(servo_node)
    return ld
