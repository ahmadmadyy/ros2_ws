from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    ur_type = LaunchConfiguration("ur_type")
    launch_rviz = LaunchConfiguration("launch_rviz")
    launch_servo = LaunchConfiguration("launch_servo")

    moveit = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution(
                [FindPackageShare("ur5e_robotiq_moveit_config"), "launch", "ur_moveit.launch.py"]
            )
        ),
        launch_arguments={
            "ur_type": ur_type,
            "launch_rviz": launch_rviz,
            "launch_servo": launch_servo,
            "use_sim_time": "true",
        }.items(),
    )

    ld = LaunchDescription()
    ld.add_action(DeclareLaunchArgument("ur_type", default_value="ur5e"))
    ld.add_action(DeclareLaunchArgument("launch_rviz", default_value="true"))
    ld.add_action(DeclareLaunchArgument("launch_servo", default_value="false"))
    ld.add_action(moveit)
    return ld
