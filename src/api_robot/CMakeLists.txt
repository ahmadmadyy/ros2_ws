cmake_minimum_required(VERSION 3.8)
project(api_robot)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rclcpp_action REQUIRED)
find_package(std_msgs REQUIRED)
find_package(trajectory_msgs REQUIRED)
find_package(control_msgs REQUIRED)
find_package(moveit_ros_planning_interface REQUIRED)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

add_executable(joint_slider_teleop src/joint_slider_teleop.cpp)
ament_target_dependencies(joint_slider_teleop
  rclcpp
  rclcpp_action
  std_msgs
  trajectory_msgs
  control_msgs
)

add_executable(joint_space_demo src/joint_space_demo.cpp)
ament_target_dependencies(joint_space_demo
  rclcpp
  rclcpp_action
  moveit_ros_planning_interface
  control_msgs
  trajectory_msgs
)

add_executable(joint_space_plan_only src/joint_space_plan_only.cpp)
ament_target_dependencies(joint_space_plan_only
  rclcpp
  moveit_ros_planning_interface
)

add_executable(goal_pose_plan_only src/goal_pose_plan_only.cpp)
ament_target_dependencies(goal_pose_plan_only
  rclcpp
  moveit_ros_planning_interface
)

add_executable(joint_space_plan_exec src/joint_space_plan_exec.cpp)
ament_target_dependencies(joint_space_plan_exec
  rclcpp
  moveit_ros_planning_interface
)

add_executable(gripper_control src/gripper_control.cpp)
ament_target_dependencies(gripper_control
  rclcpp
  rclcpp_action
  control_msgs
)


install(TARGETS
  joint_slider_teleop
  joint_space_demo
  joint_space_plan_only
  goal_pose_plan_only
  joint_space_plan_exec
  gripper_control
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
