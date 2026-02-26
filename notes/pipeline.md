STOP everything
# Kill robot_agent (FastAPI + ROS2 node)
pkill -f "robot_agent" 2>/dev/null

# Kill vLLM / Cosmos server
pkill -f "vllm" 2>/dev/null
pkill -f "Cosmos-Reason2" 2>/dev/null

# Kill ROS2 bringup / UR driver nodes
pkill -f "ur_robot_driver" 2>/dev/null
pkill -f "ros2 launch" 2>/dev/null

# Kill any leftover python3 ROS2 processes
pkill -f "robot_bringup" 2>/dev/null

# Clean up ROS2 daemon (optional, ensures clean state)
ros2 daemon stop 2>/dev/null; sleep 1; ros2 daemon start

Terminal 1 — Cosmos / vLLM

python3 -m vllm.entrypoints.openai.api_server \
  --model nvidia/Cosmos-Reason2-8B \
  --served-model-name nvidia/Cosmos-Reason2-8B \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 16384 \
  --trust-remote-code \
  --gpu-memory-utilization 0.85

