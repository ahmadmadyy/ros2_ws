cmake_minimum_required(VERSION 3.8)
project(ur5e_robotiq_moveit_config)

find_package(ament_cmake REQUIRED)

# Install launch and config directories into share/<pkg> so ros2 launch can find them
install(DIRECTORY
  launch
  config
  srdf
  urdf
  DESTINATION share/${PROJECT_NAME}
)

install(PROGRAMS
  scripts/publish_robot_description.py
  DESTINATION lib/${PROJECT_NAME}
)


ament_package()
