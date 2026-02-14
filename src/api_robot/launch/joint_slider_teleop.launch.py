from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def generate_launch_description():
    arm_action = LaunchConfiguration("arm_action")
    gripper_action = LaunchConfiguration("gripper_action")
    move_time_sec = LaunchConfiguration("move_time_sec")

    return LaunchDescription([
        DeclareLaunchArgument(
            "arm_action",
            default_value="/scaled_joint_trajectory_controller/follow_joint_trajectory",
            description="FollowJointTrajectory action name for the arm controller",
        ),
        DeclareLaunchArgument(
            "gripper_action",
            default_value="/robotiq_gripper_controller/follow_joint_trajectory",
            description="FollowJointTrajectory action name for the gripper controller",
        ),
        DeclareLaunchArgument(
            "move_time_sec",
            default_value="0.75",
            description="Trajectory point time_from_start in seconds",
        ),

        Node(
            package="api_robot",
            executable="joint_slider_teleop",
            name="joint_slider_teleop",
            output="screen",
            parameters=[{
                "arm_action": arm_action,
                "gripper_action": gripper_action,
                "move_time_sec": move_time_sec,
                # If you ever need to override joints, uncomment:
                # "arm_joints": [
                #   "shoulder_pan_joint","shoulder_lift_joint","elbow_joint",
                #   "wrist_1_joint","wrist_2_joint","wrist_3_joint"
                # ],
                # "gripper_joint": "rq_robotiq_85_left_knuckle_joint",
            }],
        ),
    ])
