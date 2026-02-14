#include <moveit/move_group_interface/move_group_interface.h>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>

#include <control_msgs/action/follow_joint_trajectory.hpp>
#include <trajectory_msgs/msg/joint_trajectory_point.hpp>

#include <chrono>
#include <memory>
#include <thread>
#include <vector>
#include <string>

using namespace std::chrono_literals;

static const rclcpp::Logger LOGGER = rclcpp::get_logger("api_robot_joint_space_demo");

class JointSpaceDemo
{
public:
  using MoveGroupInterface = moveit::planning_interface::MoveGroupInterface;
  using Plan = MoveGroupInterface::Plan;

  using FJT = control_msgs::action::FollowJointTrajectory;
  using GoalHandleFJT = rclcpp_action::ClientGoalHandle<FJT>;

  explicit JointSpaceDemo(const rclcpp::Node::SharedPtr& base_node)
  : base_node_(base_node)
  {
    RCLCPP_INFO(LOGGER, "Starting JointSpaceDemo (MoveIt arm + ros2_control gripper action)...");

    // MoveIt client node
    rclcpp::NodeOptions opts;
    opts.automatically_declare_parameters_from_overrides(true);
    moveit_node_ = rclcpp::Node::make_shared("api_robot_moveit_client", opts);

    // Action client node (can reuse same node, but keep it simple)
    action_node_ = rclcpp::Node::make_shared("api_robot_gripper_client", opts);

    // Spin both nodes
    executor_.add_node(moveit_node_);
    executor_.add_node(action_node_);
    spin_thread_ = std::thread([this]() { executor_.spin(); });

    // Params
    arm_group_name_ = moveit_node_->declare_parameter<std::string>("arm_group", "ur_manipulator");

    arm_target_ = moveit_node_->declare_parameter<std::vector<double>>(
      "arm_joint_target",
      std::vector<double>{0.0, -2.3562, 1.5708, -1.5708, -1.5708, 0.0}
    );

    // Gripper controller + joint
    gripper_action_name_ = action_node_->declare_parameter<std::string>(
      "gripper_action",
      "/robotiq_gripper_controller/follow_joint_trajectory"
    );
    gripper_joint_name_ = action_node_->declare_parameter<std::string>(
      "gripper_joint",
      "rq_robotiq_85_left_knuckle_joint"
    );

    // Robotiq: 0.0 ~ open, ~0.79 close (adjust if needed)
    gripper_open_  = action_node_->declare_parameter<double>("gripper_open", 0.0);
    gripper_close_ = action_node_->declare_parameter<double>("gripper_close", 0.79);

    // MoveIt arm interface
    arm_mgi_ = std::make_shared<MoveGroupInterface>(moveit_node_, arm_group_name_);
    arm_mgi_->setStartStateToCurrentState();
    arm_mgi_->setMaxVelocityScalingFactor(0.3);
    arm_mgi_->setMaxAccelerationScalingFactor(0.3);

    // Gripper action client
    gripper_client_ = rclcpp_action::create_client<FJT>(action_node_, gripper_action_name_);

    RCLCPP_INFO(LOGGER, "Arm group: %s", arm_group_name_.c_str());
    RCLCPP_INFO(LOGGER, "Gripper action: %s", gripper_action_name_.c_str());
    RCLCPP_INFO(LOGGER, "Gripper joint: %s", gripper_joint_name_.c_str());
    RCLCPP_INFO(LOGGER, "Initialized.");
  }

  ~JointSpaceDemo()
  {
    executor_.cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
    RCLCPP_INFO(LOGGER, "JointSpaceDemo terminated.");
  }

  void run()
  {
    // Wait for MoveGroup to be ready (it usually is, but be safe)
    std::this_thread::sleep_for(500ms);

    // 1) Move arm via MoveIt
    RCLCPP_INFO(LOGGER, "Moving arm to joint target...");
    if (!move_arm_joints(arm_target_)) {
      RCLCPP_ERROR(LOGGER, "Arm motion failed. Aborting demo.");
      return;
    }

    // 2) Open gripper via action
    RCLCPP_INFO(LOGGER, "Opening gripper...");
    if (!send_gripper_goal(gripper_open_, 1.0)) {
      RCLCPP_ERROR(LOGGER, "Gripper open failed. Aborting demo.");
      return;
    }

    std::this_thread::sleep_for(500ms);

    // 3) Close gripper via action
    RCLCPP_INFO(LOGGER, "Closing gripper...");
    if (!send_gripper_goal(gripper_close_, 1.0)) {
      RCLCPP_ERROR(LOGGER, "Gripper close failed.");
      return;
    }

    RCLCPP_INFO(LOGGER, "Demo complete.");
  }

private:
  rclcpp::Node::SharedPtr base_node_;
  rclcpp::Node::SharedPtr moveit_node_;
  rclcpp::Node::SharedPtr action_node_;

  rclcpp::executors::SingleThreadedExecutor executor_;
  std::thread spin_thread_;

  std::string arm_group_name_;
  std::vector<double> arm_target_;

  std::shared_ptr<MoveGroupInterface> arm_mgi_;

  std::string gripper_action_name_;
  std::string gripper_joint_name_;
  double gripper_open_{0.0};
  double gripper_close_{0.79};

  rclcpp_action::Client<FJT>::SharedPtr gripper_client_;

  bool move_arm_joints(const std::vector<double>& joints)
  {
    if (joints.size() != 6) {
      RCLCPP_ERROR(LOGGER, "arm_joint_target must have 6 values, got %zu", joints.size());
      return false;
    }

    arm_mgi_->setStartStateToCurrentState();
    arm_mgi_->setJointValueTarget(joints);

    Plan plan;
    auto ok = (arm_mgi_->plan(plan) == moveit::core::MoveItErrorCode::SUCCESS);
    if (!ok) {
      RCLCPP_ERROR(LOGGER, "Arm planning failed.");
      return false;
    }

    ok = (arm_mgi_->execute(plan) == moveit::core::MoveItErrorCode::SUCCESS);
    if (!ok) {
      RCLCPP_ERROR(LOGGER, "Arm execution failed.");
      return false;
    }
    return true;
  }

  bool send_gripper_goal(double position, double seconds)
  {
    if (!gripper_client_->wait_for_action_server(3s)) {
      RCLCPP_ERROR(LOGGER, "Gripper action server not available: %s", gripper_action_name_.c_str());
      return false;
    }

    FJT::Goal goal;
    goal.trajectory.joint_names = { gripper_joint_name_ };

    trajectory_msgs::msg::JointTrajectoryPoint pt;
    pt.positions = { position };
    pt.time_from_start = rclcpp::Duration::from_seconds(seconds);
    goal.trajectory.points.push_back(pt);

    auto send_goal_options = rclcpp_action::Client<FJT>::SendGoalOptions();

    auto goal_future = gripper_client_->async_send_goal(goal, send_goal_options);
    if (rclcpp::spin_until_future_complete(action_node_, goal_future, 3s) !=
        rclcpp::FutureReturnCode::SUCCESS)
    {
      RCLCPP_ERROR(LOGGER, "Failed to send gripper goal.");
      return false;
    }

    auto goal_handle = goal_future.get();
    if (!goal_handle) {
      RCLCPP_ERROR(LOGGER, "Gripper goal was rejected.");
      return false;
    }

    auto result_future = gripper_client_->async_get_result(goal_handle);
    if (rclcpp::spin_until_future_complete(action_node_, result_future, 10s) !=
        rclcpp::FutureReturnCode::SUCCESS)
    {
      RCLCPP_ERROR(LOGGER, "Timed out waiting for gripper result.");
      return false;
    }

    auto result = result_future.get();
    if (result.code != rclcpp_action::ResultCode::SUCCEEDED) {
      RCLCPP_ERROR(LOGGER, "Gripper action failed with code %d", (int)result.code);
      return false;
    }

    return true;
  }
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);
  auto base_node = std::make_shared<rclcpp::Node>("api_robot_joint_space_demo");
  JointSpaceDemo demo(base_node);

  // Give move_group time to come up if launched together
  std::this_thread::sleep_for(std::chrono::seconds(2));

  demo.run();
  rclcpp::shutdown();
  return 0;
}
