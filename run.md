# Robot Agent — Start / Stop Commands

All commands assume working directory is `~/ros2_ws`.

---

## STOP everything (run in any terminal)

```bash
# Kill robot_agent (FastAPI + ROS2)
pkill -f "robot_agent" 2>/dev/null

# Kill vLLM / Cosmos server
pkill -f "vllm" 2>/dev/null
pkill -f "Cosmos-Reason2" 2>/dev/null

# Kill UR5e bringup (move_group, ros2_control, RViz)
pkill -f "ur_robot_driver" 2>/dev/null
pkill -f "move_group" 2>/dev/null
pkill -f "ros2_control_node" 2>/dev/null
pkill -f "rviz2" 2>/dev/null
pkill -f "ros2 launch" 2>/dev/null

# Reset ROS2 daemon (clears stale DDS participants)
ros2 daemon stop 2>/dev/null; sleep 1; ros2 daemon start

# Verify everything is dead
ps aux | grep -E "vllm|robot_agent|ur_robot|move_group|ros2_control" | grep -v grep
```

---

## START — 4 separate terminals

### Terminal 1 — Cosmos / vLLM  (start first, wait for "Application startup complete")

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model nvidia/Cosmos-Reason2-8B \
  --served-model-name nvidia/Cosmos-Reason2-8B \
  --port 8000 \
  --dtype bfloat16 \
  --max-model-len 16384 \
  --trust-remote-code \
  --gpu-memory-utilization 0.85
```

Quick check (once running):
```bash
curl -s http://localhost:8000/v1/models | python3 -m json.tool
```

---

### Terminal 2 — UR5e bringup + MoveIt + RViz

```bash
source /opt/ros/jazzy/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash
ros2 launch ur5e_robotiq_moveit_config bringup.launch.py
```

Wait until you see `"You can start planning now!"` in the bringup output.

---

### Terminal 3 — robot_agent (FastAPI + ROS2 node)

```bash
source /opt/ros/jazzy/setup.bash
source /home/ubuntu/ros2_ws/install/setup.bash

# Reinstall to pick up latest source changes
pip3 install /home/ubuntu/ros2_ws/src/robot_agent --break-system-packages -q

ros2 run robot_agent robot_agent
```

Quick health check (once running):
```bash
curl -s http://localhost:8080/api/v1/health | python3 -m json.tool
```

---

### Terminal 4 — Run pick + Cosmos/Llama analysis

**Step 1 — trigger the pick:**
```bash
curl -s -X POST http://localhost:8080/api/v1/skill/pick \
  -H "Content-Type: application/json" \
  -d '{
    "params": {
      "pose": {"x":0.45,"y":0.00,"z":0.26,"qx":1.0,"qy":0.0,"qz":0.0,"qw":0.0},
      "approach_height": 0.18,
      "retreat_height":  0.18,
      "grasp_gripper_position": 0.57,
      "object_id": "screwdriver"
    }
  }' | python3 -m json.tool
```

Wait for `"success": true` in the response.

**Step 2 — run Cosmos + Llama analysis on the recorded trace:**
```bash
python3 /home/ubuntu/ros2_ws/test_cosmos_pipeline.py
```

This will:
1. Fetch the latest in-memory trace from the agent
2. Send it to Cosmos Reason2 (Prompt 1 — trajectory analysis)
3. Send Cosmos output to Llama (Prompt 2 — judge / score)
4. Print both outputs in full
5. Save full JSON to `analysis_<trace_id>.json`

Use a specific trace by ID:
```bash
python3 /home/ubuntu/ros2_ws/test_cosmos_pipeline.py --trace-id <trace_id>
```

Test Llama judge only (skip Cosmos):
```bash
python3 /home/ubuntu/ros2_ws/test_cosmos_pipeline.py \
  --cosmos-analysis "The robot approached from above and grasped successfully."
```

---

## Useful one-liners

```bash
# List all recorded traces in the agent
curl -s http://localhost:8080/api/v1/traces | python3 -m json.tool

# Check which Cosmos model the agent is using
curl -s http://localhost:8080/api/v1/model | python3 -m json.tool

# Override the model at runtime (no restart needed)
curl -s -X POST "http://localhost:8080/api/v1/model?model=nvidia/Cosmos-Reason2-8B"

# Get current robot joint state
curl -s http://localhost:8080/api/v1/state | python3 -m json.tool

# Send robot home
curl -s -X POST http://localhost:8080/api/v1/skill/home \
  -H "Content-Type: application/json" -d '{"params":{}}'
```

---

## Notes

- **IK branch**: The IK solver may return a "back" configuration (`shoulder_pan ≈ −2.84`).
  The pick skill now automatically retries with a forward-approach seed (`shoulder_pan = 0`)
  so OMPL always gets a reachable joint goal close to the home configuration.
- **Cosmos cold-start**: First inference after starting vLLM takes ~30–60 s (GPU warm-up).
  Subsequent calls are ~10–15 s.
- **Ollama** (for Llama judge) runs as a system service. Check with:
  `curl -s http://localhost:11434/api/tags | python3 -m json.tool`
