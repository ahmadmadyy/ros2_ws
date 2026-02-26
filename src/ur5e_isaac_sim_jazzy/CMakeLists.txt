cmake_minimum_required(VERSION 3.8)
project(ur5e_isaac_sim_jazzy)

find_package(ament_cmake REQUIRED)

install(DIRECTORY
  launch
  config
  DESTINATION share/${PROJECT_NAME}
)

install(FILES
  README.md
  DESTINATION share/${PROJECT_NAME}
)

install(PROGRAMS
  scripts/launch_isaac_sim_ros2_jazzy.sh
  scripts/install_isaac_sim_workstation.sh
  scripts/joint_state_to_isaac_bridge.py
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
