from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    ur_type = LaunchConfiguration("ur_type")
    launch_rviz = LaunchConfiguration("launch_rviz")
    launch_servo = LaunchConfiguration("launch_servo")
    source_topic = LaunchConfiguration("source_topic")
    command_topic = LaunchConfiguration("command_topic")

    bringup = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution(
                [FindPackageShare("ur5e_isaac_sim_jazzy"), "launch", "isaac_moveit_bringup.launch.py"]
            )
        ),
        launch_arguments={
            "ur_type": ur_type,
            "launch_rviz": launch_rviz,
            "launch_servo": launch_servo,
        }.items(),
    )

    bridge = Node(
        package="ur5e_isaac_sim_jazzy",
        executable="joint_state_to_isaac_bridge.py",
        name="joint_state_to_isaac_bridge",
        output="screen",
        parameters=[
            {
                "source_topic": source_topic,
                "command_topic": command_topic,
                "joint_names": [
                    "shoulder_pan_joint",
                    "shoulder_lift_joint",
                    "elbow_joint",
                    "wrist_1_joint",
                    "wrist_2_joint",
                    "wrist_3_joint",
                    "rq_robotiq_85_left_knuckle_joint",
                ],
            }
        ],
    )

    ld = LaunchDescription()
    ld.add_action(DeclareLaunchArgument("ur_type", default_value="ur5e"))
    ld.add_action(DeclareLaunchArgument("launch_rviz", default_value="true"))
    ld.add_action(DeclareLaunchArgument("launch_servo", default_value="false"))
    ld.add_action(DeclareLaunchArgument("source_topic", default_value="/joint_states"))
    ld.add_action(DeclareLaunchArgument("command_topic", default_value="/isaac_joint_commands"))
    ld.add_action(bringup)
    ld.add_action(bridge)
    return ld
