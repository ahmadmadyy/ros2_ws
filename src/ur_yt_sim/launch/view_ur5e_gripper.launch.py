from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import Command, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

def generate_launch_description():
    description_pkg = FindPackageShare("ur_yt_sim")

    xacro_file = PathJoinSubstitution([description_pkg, "urdf", "ur5e_gripper.urdf.xacro"])
    rviz_config = PathJoinSubstitution([description_pkg, "rviz", "ur5e_gripper.rviz"])

    name_arg = DeclareLaunchArgument("name", default_value="ur")
    tf_prefix_arg = DeclareLaunchArgument("tf_prefix", default_value="")
    ur_type_arg = DeclareLaunchArgument("ur_type", default_value="ur5e")

    robot_description = Command([
        "xacro ", xacro_file,
        " name:=", LaunchConfiguration("name"),
        " tf_prefix:=", LaunchConfiguration("tf_prefix"),
        " ur_type:=", LaunchConfiguration("ur_type"),
    ])

    return LaunchDescription([
        name_arg,
        tf_prefix_arg,
        ur_type_arg,

        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            output="screen",
            parameters=[{"robot_description": robot_description}],
        ),

        Node(
            package="joint_state_publisher_gui",
            executable="joint_state_publisher_gui",
            output="screen",
        ),

        Node(
            package="rviz2",
            executable="rviz2",
            name="rviz2",
            output="screen",
            arguments=["-d", rviz_config],
        ),
    ])
