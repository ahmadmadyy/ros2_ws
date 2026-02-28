# Robot Agent — Run Commands

---

## Stop Everything First

```bash
tmux kill-session -t robot_stack 2>/dev/null
pkill -9 -f move_group
pkill -9 -f ros2_control_node
pkill -9 -f robot_agent
pkill -9 -f "vllm serve"
pkill -9 -f "ollama serve"
source /opt/ros/jazzy/setup.bash && ros2 daemon stop && ros2 daemon start
```

---

## Option A — Launch Everything at Once

```bash
bash ~/ros2_ws/scripts/launch_all.sh --model 2b
```

---

## Option B — Launch Each Service Manually

Open a separate terminal for each command.

### Terminal 1 — ROS Bringup (MoveIt + controllers)

```bash
source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
ros2 launch ur5e_robotiq_moveit_config bringup.launch.py
```

### Terminal 2 — Cosmos Reason2 (vLLM server)

```bash
export PATH="$HOME/.local/bin:$PATH"
vllm serve nvidia/Cosmos-Reason2-2B \
  --max-model-len 16384 \
  --reasoning-parser qwen3 \
  --port 8000 \
  --gpu-memory-utilization 0.85
```

> For the 8B model replace `Cosmos-Reason2-2B` with `Cosmos-Reason2-8B`

### Terminal 3 — Ollama (Llama)

```bash
ollama serve
```

> First time only — pull the model:
> ```bash
> ollama pull llama3:latest
> ```

### Terminal 4 — Robot Agent (FastAPI)

Wait for Cosmos to print **"Application startup complete"** and bringup to be ready, then:

```bash
source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
ros2 run robot_agent robot_agent
```

---

## Run Pick and Place

```bash
source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
python3 ~/ros2_ws/tests/test_pick_place_box.py
```

Trace saved to: `~/ros2_ws/outputs/pick_trace/pick_place_box_YYYYMMDD_HHMMSS.json`

---

## Load Trace + Run Cosmos → Llama Pipeline

```bash
TRACE_FILE=$(ls -t ~/ros2_ws/outputs/pick_trace/pick_place_box_*.json | head -1) && \
TRACE_ID=$(curl -s -X POST "http://localhost:8080/api/v1/traces/load?file_path=${TRACE_FILE}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['trace_id'])") && \
echo "Loaded trace_id: $TRACE_ID" && \
curl -s --max-time 600 -X POST "http://localhost:8080/api/v1/analyze/${TRACE_ID}" \
  -H "Content-Type: application/json" \
  -d '{
    "instruction": "Pick up the small 50mm cube from x=0.35 y=0.25 and place it at x=0.35 y=-0.25",
    "object_name": "small_box (50mm cube)",
    "pick_x": 0.35, "pick_y": 0.25, "pick_z": 0.025,
    "object_dims": "cube: 50mm x 50mm x 50mm",
    "grasp_z": 0.195, "approach_height": 0.15, "retreat_height": 0.15,
    "grasp_gripper_rad": 0.325,
    "place_description": "x=0.35, y=-0.25, z=0.025",
    "scene_context": "Flat table at z=0. Screwdriver at x=0.45 y=0.0 present as obstacle."
  }' | python3 -m json.tool
```

Outputs saved to:
- `~/ros2_ws/outputs/cosmos/cosmos_<trace_id>_<datetime>.txt`
- `~/ros2_ws/outputs/eval/eval_<trace_id>_<datetime>.json`
