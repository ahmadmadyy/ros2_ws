# UR5e Isaac Sim (ROS 2 Jazzy, Isolated Package)

This package keeps Isaac Sim integration isolated from your existing packages (`ur5e_robotiq_moveit_config`, `ur_yt_sim`, `api_robot`).

## 1) Build

```bash
cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install --packages-select ur5e_isaac_sim_jazzy ur5e_robotiq_moveit_config api_robot
source install/setup.bash
```

## 2) Install Isaac Sim (workstation)

Download from NVIDIA docs:
- https://docs.isaacsim.omniverse.nvidia.com/latest/installation/download.html

Install archive (`.zip` or `.tar.gz`) into `~/isaacsim`:

```bash
ARCHIVE_PATH=~/Downloads/isaac-sim-standalone-5.1.0-linux-x86_64.zip \
TARGET_DIR=~/isaacsim \
ros2 run ur5e_isaac_sim_jazzy install_isaac_sim_workstation.sh
```

## 3) Run Isaac Sim with ROS 2 Jazzy env

```bash
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
export ISAAC_SIM_ROOT=~/isaacsim
export ROS2_WS_SETUP=~/ros2_ws/install/setup.bash
export ISAAC_STAGE_PATH=~/isaac_stages/ur5e_robotiq.usd
ros2 run ur5e_isaac_sim_jazzy launch_isaac_sim_ros2_jazzy.sh
```

`ISAAC_STAGE_PATH` is optional but recommended. If set, Isaac opens that stage automatically at startup.

## 4) One-time stage setup (UR5e + Robotiq + graph)

Create and save a stage that contains your UR5e + Robotiq articulation. Then create this Action Graph once:

1. `On Playback Tick`
2. `ROS2 Context`
3. `ROS2 Subscribe Joint State`
4. `Articulation Controller`

Connect:
- `On Playback Tick.outputs:tick -> ROS2 Subscribe Joint State.inputs:execIn`
- `ROS2 Context.outputs:context -> ROS2 Subscribe Joint State.inputs:context`
- `ROS2 Context.outputs:context -> Articulation Controller.inputs:context`
- `ROS2 Subscribe Joint State.outputs:positionCommand -> Articulation Controller.inputs:positionCommand`
- `ROS2 Subscribe Joint State.outputs:jointNames -> Articulation Controller.inputs:jointNames`
- `On Playback Tick.outputs:tick -> Articulation Controller.inputs:execIn`

Set node parameters:
- `ROS2 Subscribe Joint State.inputs:topicName = /isaac_joint_commands`
- `Articulation Controller.inputs:robotPath = <your UR5e articulation prim path>`

Optional state publish back out:
- Add `ROS2 Publish Joint State` and set topic `/isaac_joint_states`.

Save this stage (example: `~/isaac_stages/ur5e_robotiq.usd`) and keep `ISAAC_STAGE_PATH` pointing to it.

## 5) Launch ROS bringup + bridge (single command)

This starts your existing MoveIt/ros2_control bringup and a bridge that mirrors `/joint_states` to `/isaac_joint_commands`:

```bash
source /opt/ros/jazzy/setup.bash
source ~/ros2_ws/install/setup.bash
ros2 launch ur5e_isaac_sim_jazzy isaac_moveit_bringup_with_bridge.launch.py ur_type:=ur5e
```

## 6) Run `api_robot`

Use a new terminal (after sourcing Jazzy + workspace):

Plan+execute arm:

```bash
ros2 run api_robot joint_space_plan_exec
```

Gripper open/close:

```bash
ros2 run api_robot gripper_control
```

Teleop by topic:

```bash
ros2 run api_robot joint_slider_teleop --ros-args \
  -p arm_action:=/scaled_joint_trajectory_controller/follow_joint_trajectory \
  -p gripper_action:=/gripper_controller/gripper_cmd
```

Then send targets:

```bash
ros2 topic pub -1 /api_robot/arm_target std_msgs/msg/Float64MultiArray '{data: [0.0, -1.2, 1.2, -1.57, 0.0, 0.0]}'
ros2 topic pub -1 /api_robot/gripper_target std_msgs/msg/Float64 '{data: 0.4}'
```

## 7) Verify topics are flowing

```bash
ros2 topic hz /joint_states
ros2 topic hz /isaac_joint_commands
ros2 topic echo /isaac_joint_commands --once
```

If configured, also check Isaac-published state:

```bash
ros2 topic echo /isaac_joint_states --once
```

## Notes

- This bridge is one-way (`/joint_states -> /isaac_joint_commands`) so Isaac mirrors ROS motion.
- `launch_isaac_sim_ros2_jazzy.sh` can auto-open your saved stage via `ISAAC_STAGE_PATH`, so you do not have to add the robot manually each run.
- Existing packages were not modified.

## References

- Isaac Sim install docs: https://docs.isaacsim.omniverse.nvidia.com/latest/installation/download.html
- ROS 2 in Isaac Sim: https://docs.isaacsim.omniverse.nvidia.com/latest/installation/install_ros.html
- Isaac Sim repository: https://github.com/isaac-sim/IsaacSim
