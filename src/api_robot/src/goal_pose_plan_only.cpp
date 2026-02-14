#include <rclcpp/rclcpp.hpp>
#include <moveit/move_group_interface/move_group_interface.hpp>

#include <thread>
#include <vector>

static const rclcpp::Logger LOGGER = rclcpp::get_logger("api_robot_goal_pose_plan_only");

class GoalPosePlanOnlyV2
{
public:
  using MoveGroupInterface = moveit::planning_interface::MoveGroupInterface;
  using Plan = MoveGroupInterface::Plan;

  explicit GoalPosePlanOnlyV2(const rclcpp::Node::SharedPtr& node)
  : node_(node)
  {
    RCLCPP_INFO(LOGGER, "Initializing: GoalPosePlanOnlyV2...");

    // Spin so MoveGroupInterface current state monitor works
    exec_.add_node(node_);
    spin_thread_ = std::thread([this]() { exec_.spin(); });

    // IMPORTANT: do NOT declare parameters if auto-declare is enabled in NodeOptions.
    node_->get_parameter_or("arm_group", arm_group_, std::string("ur_manipulator"));
    node_->get_parameter_or("ee_link", ee_link_, std::string("tool0"));

    node_->get_parameter_or("planning_time", planning_time_, 10.0);
    node_->get_parameter_or("num_attempts", num_attempts_, 10);

    // If provided, use absolute target pose:
    node_->get_parameter_or("use_relative_target", use_relative_target_, true);
    node_->get_parameter_or("delta_xyz", delta_xyz_, std::vector<double>({0.0, 0.0, 0.05}));

    node_->get_parameter_or("target_position", target_position_, std::vector<double>({0.35, 0.10, 0.30}));
    node_->get_parameter_or("target_quat", target_quat_, std::vector<double>({0.0, 0.0, 0.0, 1.0}));

    mgi_ = std::make_shared<MoveGroupInterface>(node_, arm_group_);

    mgi_->setPlanningTime(planning_time_);
    mgi_->setNumPlanningAttempts(num_attempts_);
    mgi_->setStartStateToCurrentState();

    // Helpful to reduce “random” fails
    mgi_->setGoalPositionTolerance(0.005);      // 5mm
    mgi_->setGoalOrientationTolerance(0.01);    // rad
    mgi_->setMaxVelocityScalingFactor(0.3);
    mgi_->setMaxAccelerationScalingFactor(0.3);

    RCLCPP_INFO(LOGGER, "Planning frame: %s", mgi_->getPlanningFrame().c_str());
    RCLCPP_INFO(LOGGER, "End effector link: %s", mgi_->getEndEffectorLink().c_str());
    RCLCPP_INFO(LOGGER, "Initialized: GoalPosePlanOnlyV2");
  }

  ~GoalPosePlanOnlyV2()
  {
    exec_.cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
  }

  void plan()
  {
    // Wait a moment so current_state_monitor gets joint_states
    rclcpp::sleep_for(std::chrono::milliseconds(700));
    mgi_->setStartStateToCurrentState();

    geometry_msgs::msg::Pose target;

    if (use_relative_target_) {
      // Take current pose and move a little bit (reachable by construction)
      auto cur = mgi_->getCurrentPose(ee_link_).pose;

      if (delta_xyz_.size() != 3) delta_xyz_ = {0.05, 0.0, 0.0};

      target = cur;
      target.position.x += delta_xyz_[0];
      target.position.y += delta_xyz_[1];
      target.position.z += delta_xyz_[2];

      RCLCPP_INFO(LOGGER, "Using relative target from current pose (delta xyz = [%.3f %.3f %.3f])",
                  delta_xyz_[0], delta_xyz_[1], delta_xyz_[2]);
      RCLCPP_INFO(LOGGER, "Current pos = [%.3f %.3f %.3f]", cur.position.x, cur.position.y, cur.position.z);
    } else {
      // Absolute pose
      if (target_position_.size() != 3 || target_quat_.size() != 4) {
        RCLCPP_ERROR(LOGGER, "target_position must be 3 values and target_quat must be 4 values");
        return;
      }
      target.position.x = target_position_[0];
      target.position.y = target_position_[1];
      target.position.z = target_position_[2];
      target.orientation.x = target_quat_[0];
      target.orientation.y = target_quat_[1];
      target.orientation.z = target_quat_[2];
      target.orientation.w = target_quat_[3];

      RCLCPP_INFO(LOGGER, "Using absolute target pose.");
    }

    RCLCPP_INFO(LOGGER, "Target pos = [%.3f %.3f %.3f]", target.position.x, target.position.y, target.position.z);
    RCLCPP_INFO(LOGGER, "Target quat= [%.3f %.3f %.3f %.3f]",
                target.orientation.x, target.orientation.y, target.orientation.z, target.orientation.w);

    mgi_->clearPoseTargets();
    mgi_->setStartStateToCurrentState();

    mgi_->setPoseTarget(target, ee_link_);

    Plan plan;
    auto ok = (mgi_->plan(plan) == moveit::core::MoveItErrorCode::SUCCESS);
    if (!ok) {
      RCLCPP_ERROR(LOGGER, "Planning FAILED (try increasing planning_time or adjusting delta/target).");
      return;
    }

    // Print something meaningful (MoveIt2 Jazzy uses plan.trajectory, not trajectory_)
    const auto& jt = plan.trajectory.joint_trajectory;
    RCLCPP_INFO(LOGGER, "Planning SUCCEEDED. joints=%zu points=%zu",
                jt.joint_names.size(), jt.points.size());
  }

private:
  rclcpp::Node::SharedPtr node_;
  rclcpp::executors::SingleThreadedExecutor exec_;
  std::thread spin_thread_;

  std::shared_ptr<MoveGroupInterface> mgi_;

  std::string arm_group_;
  std::string ee_link_;

  double planning_time_{10.0};
  int num_attempts_{10};

  bool use_relative_target_{true};
  std::vector<double> delta_xyz_;
  std::vector<double> target_position_;
  std::vector<double> target_quat_;
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);

  rclcpp::NodeOptions opts;
  opts.automatically_declare_parameters_from_overrides(true); // keep this ON
  auto node = rclcpp::Node::make_shared("goal_pose_plan_only", opts);

  GoalPosePlanOnlyV2 app(node);
  app.plan();

  rclcpp::shutdown();
  return 0;
}
