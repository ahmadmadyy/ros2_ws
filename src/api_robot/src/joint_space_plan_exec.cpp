#include <rclcpp/rclcpp.hpp>
#include <moveit/move_group_interface/move_group_interface.hpp>

#include <thread>
#include <vector>

static const rclcpp::Logger LOGGER = rclcpp::get_logger("api_robot_joint_space_plan_exec");

class JointSpacePlanExec
{
public:
  using MoveGroupInterface = moveit::planning_interface::MoveGroupInterface;
  using Plan = MoveGroupInterface::Plan;

  explicit JointSpacePlanExec(const rclcpp::Node::SharedPtr& node)
  : node_(node)
  {
    RCLCPP_INFO(LOGGER, "Initializing: JointSpacePlanExec...");

    exec_.add_node(node_);
    spin_thread_ = std::thread([this]() { exec_.spin(); });

    node_->get_parameter_or("arm_group",    arm_group_,    std::string("ur_manipulator"));
    node_->get_parameter_or("planning_time", planning_time_, 10.0);
    node_->get_parameter_or("num_attempts",  num_attempts_,  10);

    // 6 joint angles [rad]: shoulder_pan, shoulder_lift, elbow,
    //                        wrist_1, wrist_2, wrist_3
    node_->get_parameter_or(
      "arm_joint_target", arm_joint_target_,
      std::vector<double>{0.0, -2.3562, 1.5708, -1.5708, -1.5708, 0.0});

    mgi_ = std::make_shared<MoveGroupInterface>(node_, arm_group_);
    mgi_->setPlanningTime(planning_time_);
    mgi_->setNumPlanningAttempts(num_attempts_);
    mgi_->setStartStateToCurrentState();
    mgi_->setMaxVelocityScalingFactor(0.3);
    mgi_->setMaxAccelerationScalingFactor(0.3);

    RCLCPP_INFO(LOGGER, "Planning frame:    %s", mgi_->getPlanningFrame().c_str());
    RCLCPP_INFO(LOGGER, "End effector link: %s", mgi_->getEndEffectorLink().c_str());
    RCLCPP_INFO(LOGGER, "Initialized: JointSpacePlanExec");
  }

  ~JointSpacePlanExec()
  {
    exec_.cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
  }

  void run()
  {
    // Wait for current_state_monitor to receive joint_states
    rclcpp::sleep_for(std::chrono::milliseconds(700));
    mgi_->setStartStateToCurrentState();

    if (arm_joint_target_.size() != 6) {
      RCLCPP_ERROR(LOGGER, "arm_joint_target must have 6 values, got %zu",
                   arm_joint_target_.size());
      return;
    }

    RCLCPP_INFO(LOGGER, "Target joints [rad]: [%.4f  %.4f  %.4f  %.4f  %.4f  %.4f]",
                arm_joint_target_[0], arm_joint_target_[1], arm_joint_target_[2],
                arm_joint_target_[3], arm_joint_target_[4], arm_joint_target_[5]);

    mgi_->setJointValueTarget(arm_joint_target_);

    // --- Plan ---
    Plan plan;
    auto ok = (mgi_->plan(plan) == moveit::core::MoveItErrorCode::SUCCESS);
    if (!ok) {
      RCLCPP_ERROR(LOGGER, "Planning FAILED.");
      return;
    }

    const auto& jt = plan.trajectory.joint_trajectory;
    RCLCPP_INFO(LOGGER, "Planning SUCCEEDED: joints=%zu  points=%zu — executing...",
                jt.joint_names.size(), jt.points.size());

    // --- Execute ---
    ok = (mgi_->execute(plan) == moveit::core::MoveItErrorCode::SUCCESS);
    if (!ok) {
      RCLCPP_ERROR(LOGGER, "Execution FAILED.");
      return;
    }

    RCLCPP_INFO(LOGGER, "Execution SUCCEEDED.");
  }

private:
  rclcpp::Node::SharedPtr node_;
  rclcpp::executors::SingleThreadedExecutor exec_;
  std::thread spin_thread_;

  std::shared_ptr<MoveGroupInterface> mgi_;

  std::string arm_group_;
  double planning_time_{10.0};
  int num_attempts_{10};
  std::vector<double> arm_joint_target_;
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);

  rclcpp::NodeOptions opts;
  opts.automatically_declare_parameters_from_overrides(true);
  auto node = rclcpp::Node::make_shared("joint_space_plan_exec", opts);

  JointSpacePlanExec app(node);
  app.run();

  rclcpp::shutdown();
  return 0;
}
