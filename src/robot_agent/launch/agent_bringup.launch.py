import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    config_file = os.path.join(
        get_package_share_directory('robot_agent'),
        'config',
        'agent_params.yaml',
    )

    return LaunchDescription([
        Node(
            package='robot_agent',
            executable='robot_agent',
            name='robot_agent',
            output='screen',
            parameters=[config_file],
        ),
    ])
