#include <chrono>
#include <memory>
#include <string>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp_action/rclcpp_action.hpp"

#include "std_msgs/msg/float64.hpp"
#include "std_msgs/msg/float64_multi_array.hpp"

#include "control_msgs/action/follow_joint_trajectory.hpp"
#include "trajectory_msgs/msg/joint_trajectory.hpp"
#include "trajectory_msgs/msg/joint_trajectory_point.hpp"

using namespace std::chrono_literals;

class JointSliderTeleop : public rclcpp::Node
{
public:
  using FJT = control_msgs::action::FollowJointTrajectory;
  using GoalHandleFJT = rclcpp_action::ClientGoalHandle<FJT>;

  JointSliderTeleop()
  : Node("joint_slider_teleop")
  {
    // Parameters (override in launch if you want)
    arm_action_name_ = this->declare_parameter<std::string>(
      "arm_action", "/scaled_joint_trajectory_controller/follow_joint_trajectory");
    gripper_action_name_ = this->declare_parameter<std::string>(
      "gripper_action", "/robotiq_gripper_controller/follow_joint_trajectory");

    move_time_sec_ = this->declare_parameter<double>("move_time_sec", 0.75);

    arm_joints_ = this->declare_parameter<std::vector<std::string>>(
      "arm_joints",
      {"shoulder_pan_joint","shoulder_lift_joint","elbow_joint","wrist_1_joint","wrist_2_joint","wrist_3_joint"});

    gripper_joint_ = this->declare_parameter<std::string>(
      "gripper_joint", "rq_robotiq_85_left_knuckle_joint");

    arm_client_ = rclcpp_action::create_client<FJT>(this, arm_action_name_);
    gripper_client_ = rclcpp_action::create_client<FJT>(this, gripper_action_name_);

    arm_sub_ = this->create_subscription<std_msgs::msg::Float64MultiArray>(
      "/api_robot/arm_target", 10,
      std::bind(&JointSliderTeleop::onArmTarget, this, std::placeholders::_1));

    gripper_sub_ = this->create_subscription<std_msgs::msg::Float64>(
      "/api_robot/gripper_target", 10,
      std::bind(&JointSliderTeleop::onGripperTarget, this, std::placeholders::_1));

    RCLCPP_INFO(get_logger(), "Ready.");
    RCLCPP_INFO(get_logger(), "Arm action: %s", arm_action_name_.c_str());
    RCLCPP_INFO(get_logger(), "Gripper action: %s", gripper_action_name_.c_str());
  }

private:
  void waitForServer(const rclcpp_action::Client<FJT>::SharedPtr& client, const std::string& name)
  {
    if (!client->wait_for_action_server(3s)) {
      RCLCPP_WARN(get_logger(), "Action server not available yet: %s", name.c_str());
    }
  }

  void onArmTarget(const std_msgs::msg::Float64MultiArray::SharedPtr msg)
  {
    if (msg->data.size() != 6) {
      RCLCPP_ERROR(get_logger(), "arm_target must have 6 values. Got %zu", msg->data.size());
      return;
    }
    waitForServer(arm_client_, arm_action_name_);

    trajectory_msgs::msg::JointTrajectory traj;
    traj.joint_names = arm_joints_;

    trajectory_msgs::msg::JointTrajectoryPoint pt;
    pt.positions.assign(msg->data.begin(), msg->data.end());
    pt.time_from_start = rclcpp::Duration::from_seconds(move_time_sec_);
    traj.points.push_back(pt);

    FJT::Goal goal;
    goal.trajectory = traj;

    auto opts = rclcpp_action::Client<FJT>::SendGoalOptions();
    opts.result_callback = [this](const GoalHandleFJT::WrappedResult& result) {
      if (result.code != rclcpp_action::ResultCode::SUCCEEDED) {
        RCLCPP_WARN(this->get_logger(), "Arm goal finished with code %d", (int)result.code);
      }
    };

    arm_client_->async_send_goal(goal, opts);
  }

  void onGripperTarget(const std_msgs::msg::Float64::SharedPtr msg)
  {
    waitForServer(gripper_client_, gripper_action_name_);

    trajectory_msgs::msg::JointTrajectory traj;
    traj.joint_names = {gripper_joint_};

    trajectory_msgs::msg::JointTrajectoryPoint pt;
    pt.positions = {msg->data};
    pt.time_from_start = rclcpp::Duration::from_seconds(move_time_sec_);
    traj.points.push_back(pt);

    FJT::Goal goal;
    goal.trajectory = traj;

    auto opts = rclcpp_action::Client<FJT>::SendGoalOptions();
    opts.result_callback = [this](const GoalHandleFJT::WrappedResult& result) {
      if (result.code != rclcpp_action::ResultCode::SUCCEEDED) {
        RCLCPP_WARN(this->get_logger(), "Gripper goal finished with code %d", (int)result.code);
      }
    };

    gripper_client_->async_send_goal(goal, opts);
  }

  std::string arm_action_name_;
  std::string gripper_action_name_;
  double move_time_sec_;

  std::vector<std::string> arm_joints_;
  std::string gripper_joint_;

  rclcpp_action::Client<FJT>::SharedPtr arm_client_;
  rclcpp_action::Client<FJT>::SharedPtr gripper_client_;

  rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr arm_sub_;
  rclcpp::Subscription<std_msgs::msg::Float64>::SharedPtr gripper_sub_;
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<JointSliderTeleop>());
  rclcpp::shutdown();
  return 0;
}
