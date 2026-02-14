#include <moveit/move_group_interface/move_group_interface.hpp>
#include <rclcpp/rclcpp.hpp>

#include <memory>
#include <thread>
#include <vector>

static const rclcpp::Logger LOGGER = rclcpp::get_logger("api_robot_joint_space_plan_only");
static const std::string PLANNING_GROUP = "ur_manipulator";

class JointSpacePlanOnly
{
public:
  using MoveGroupInterface = moveit::planning_interface::MoveGroupInterface;
  using JointModelGroup = moveit::core::JointModelGroup;
  using RobotStatePtr = moveit::core::RobotStatePtr;
  using Plan = MoveGroupInterface::Plan;

  JointSpacePlanOnly()
  {
    RCLCPP_INFO(LOGGER, "Starting JointSpacePlanOnly (plan only)...");

    rclcpp::NodeOptions opts;
    opts.automatically_declare_parameters_from_overrides(true);

    // Node for MoveGroupInterface
    moveit_node_ = rclcpp::Node::make_shared("api_robot_moveit_client", opts);

    executor_.add_node(moveit_node_);
    spin_thread_ = std::thread([this]() { executor_.spin(); });

    move_group_ = std::make_shared<MoveGroupInterface>(moveit_node_, PLANNING_GROUP);

    joint_model_group_ =
      move_group_->getCurrentState()->getJointModelGroup(PLANNING_GROUP);

    RCLCPP_INFO(LOGGER, "Planning Frame: %s", move_group_->getPlanningFrame().c_str());
    RCLCPP_INFO(LOGGER, "End Effector Link: %s", move_group_->getEndEffectorLink().c_str());

    auto groups = move_group_->getJointModelGroupNames();
    RCLCPP_INFO(LOGGER, "Available Planning Groups:");
    for (size_t i = 0; i < groups.size(); ++i) {
      RCLCPP_INFO(LOGGER, "  [%zu] %s", i, groups[i].c_str());
    }

    current_state_ = move_group_->getCurrentState(10.0);
    current_state_->copyJointGroupPositions(joint_model_group_, joint_positions_);
    move_group_->setStartStateToCurrentState();

    RCLCPP_INFO(LOGGER, "Initialized. About to plan (NO execute).");
  }

  ~JointSpacePlanOnly()
  {
    executor_.cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
    RCLCPP_INFO(LOGGER, "Stopped JointSpacePlanOnly.");
  }

  void plan()
  {
    // Target (same values you were using)
    set_joint_target(0.0, -2.3562, 1.5708, -1.5708, -1.5708, 0.0);

    Plan plan;
    auto ok = (move_group_->plan(plan) == moveit::core::MoveItErrorCode::SUCCESS);

    if (ok) {
      RCLCPP_INFO(LOGGER, "Planning SUCCESS. Points: %zu",
                  plan.trajectory.joint_trajectory.points.size());
      RCLCPP_INFO(LOGGER, "NOTE: This node does NOT execute. Only plans.");
    } else {
      RCLCPP_ERROR(LOGGER, "Planning FAILED.");
    }
  }

private:
  rclcpp::Node::SharedPtr moveit_node_;
  rclcpp::executors::SingleThreadedExecutor executor_;
  std::thread spin_thread_;

  std::shared_ptr<MoveGroupInterface> move_group_;
  const JointModelGroup* joint_model_group_{nullptr};
  RobotStatePtr current_state_;
  std::vector<double> joint_positions_;

  void set_joint_target(double a0, double a1, double a2, double a3, double a4, double a5)
  {
    if (joint_positions_.size() < 6) joint_positions_.resize(6);
    joint_positions_[0] = a0;
    joint_positions_[1] = a1;
    joint_positions_[2] = a2;
    joint_positions_[3] = a3;
    joint_positions_[4] = a4;
    joint_positions_[5] = a5;
    move_group_->setJointValueTarget(joint_positions_);
  }
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);

  JointSpacePlanOnly node;
  node.plan();

  rclcpp::shutdown();
  return 0;
}
