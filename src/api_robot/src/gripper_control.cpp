#include <rclcpp/rclcpp.hpp>
#include <rclcpp_action/rclcpp_action.hpp>
#include <control_msgs/action/gripper_command.hpp>

#include <thread>

using namespace std::chrono_literals;

static const rclcpp::Logger LOGGER = rclcpp::get_logger("api_robot_gripper_control");

class GripperControl
{
public:
  using GripperCommand = control_msgs::action::GripperCommand;
  using GoalHandle     = rclcpp_action::ClientGoalHandle<GripperCommand>;

  explicit GripperControl(const rclcpp::Node::SharedPtr& node)
  : node_(node)
  {
    RCLCPP_INFO(LOGGER, "Initializing: GripperControl...");

    exec_.add_node(node_);
    spin_thread_ = std::thread([this]() { exec_.spin(); });

    node_->get_parameter_or("gripper_action",
      gripper_action_, std::string("/gripper_controller/gripper_cmd"));
    node_->get_parameter_or("gripper_open",  gripper_open_,  0.0);
    node_->get_parameter_or("gripper_close", gripper_close_, 0.78);
    node_->get_parameter_or("max_effort",    max_effort_,    0.0);

    client_ = rclcpp_action::create_client<GripperCommand>(node_, gripper_action_);

    RCLCPP_INFO(LOGGER, "Action:  %s", gripper_action_.c_str());
    RCLCPP_INFO(LOGGER, "Open:    %.3f   Close: %.3f   Effort: %.1f",
                gripper_open_, gripper_close_, max_effort_);
    RCLCPP_INFO(LOGGER, "Initialized: GripperControl");
  }

  ~GripperControl()
  {
    exec_.cancel();
    if (spin_thread_.joinable()) spin_thread_.join();
  }

  void run()
  {
    if (!client_->wait_for_action_server(5s)) {
      RCLCPP_ERROR(LOGGER, "Action server %s not available.", gripper_action_.c_str());
      return;
    }

    // Close gripper
    RCLCPP_INFO(LOGGER, "Closing gripper...");
    if (!send_goal(gripper_close_)) {
      RCLCPP_ERROR(LOGGER, "Close FAILED.");
      return;
    }
    RCLCPP_INFO(LOGGER, "Gripper closed.");

    rclcpp::sleep_for(3s);

    // Open gripper
    RCLCPP_INFO(LOGGER, "Opening gripper...");
    if (!send_goal(gripper_open_)) {
      RCLCPP_ERROR(LOGGER, "Open FAILED.");
      return;
    }
    RCLCPP_INFO(LOGGER, "Gripper opened.");

    RCLCPP_INFO(LOGGER, "Gripper control complete.");
  }

private:
  rclcpp::Node::SharedPtr node_;
  rclcpp::executors::SingleThreadedExecutor exec_;
  std::thread spin_thread_;

  rclcpp_action::Client<GripperCommand>::SharedPtr client_;

  std::string gripper_action_;
  double gripper_open_{0.0};
  double gripper_close_{0.78};
  double max_effort_{0.0};

  bool send_goal(double position)
  {
    GripperCommand::Goal goal;
    goal.command.position   = position;
    goal.command.max_effort = max_effort_;

    // Send goal — the executor is already spinning, so just wait on the future
    auto goal_future = client_->async_send_goal(goal);
    if (goal_future.wait_for(5s) != std::future_status::ready) {
      RCLCPP_ERROR(LOGGER, "Failed to send goal.");
      return false;
    }

    auto goal_handle = goal_future.get();
    if (!goal_handle) {
      RCLCPP_ERROR(LOGGER, "Goal rejected.");
      return false;
    }

    // Wait for result
    auto result_future = client_->async_get_result(goal_handle);
    if (result_future.wait_for(10s) != std::future_status::ready) {
      RCLCPP_ERROR(LOGGER, "Timed out waiting for result.");
      return false;
    }

    auto result = result_future.get();
    if (result.code != rclcpp_action::ResultCode::SUCCEEDED) {
      RCLCPP_ERROR(LOGGER, "Action failed (code %d).", static_cast<int>(result.code));
      return false;
    }

    RCLCPP_INFO(LOGGER, "Reached position: %.3f", result.result->position);
    return true;
  }
};

int main(int argc, char** argv)
{
  rclcpp::init(argc, argv);

  rclcpp::NodeOptions opts;
  opts.automatically_declare_parameters_from_overrides(true);
  auto node = rclcpp::Node::make_shared("gripper_control", opts);

  GripperControl app(node);
  app.run();

  rclcpp::shutdown();
  return 0;
}
