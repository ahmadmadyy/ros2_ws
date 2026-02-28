#!/usr/bin/env bash
# =============================================================================
# launch_all.sh — Single-command launcher for the full robot agent stack.
#
# Startup order:
#   1. bringup  — ROS2 MoveIt + controllers (starts immediately)
#   2. cosmos   — Cosmos Reason2 vLLM server (starts immediately)
#   3. ollama   — Ollama / Llama server (starts immediately)
#   4. agent    — robot_agent FastAPI (waits until bringup + Cosmos are ready)
#
# Session name: robot_stack
# Windows:  Ctrl+b 0=bringup  1=cosmos  2=ollama  3=agent
# Detach:   Ctrl+b d
# Kill:     tmux kill-session -t robot_stack
#
# Usage:
#   bash ~/ros2_ws/scripts/launch_all.sh [--model 2b|8b]
# =============================================================================

SESSION="robot_stack"
WS="$HOME/ros2_ws"
ROS_SETUP="/opt/ros/jazzy/setup.bash"
WS_SETUP="$WS/install/setup.bash"

# Default Cosmos model
COSMOS_MODEL="nvidia/Cosmos-Reason2-8B"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            shift
            case "$1" in
                2b) COSMOS_MODEL="nvidia/Cosmos-Reason2-2B" ;;
                8b) COSMOS_MODEL="nvidia/Cosmos-Reason2-8B" ;;
                *)  echo "Unknown model: $1 (use 2b or 8b)"; exit 1 ;;
            esac
            shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Kill stale processes from previous runs
# ---------------------------------------------------------------------------
echo "[launch_all] Cleaning up stale processes..."

tmux kill-session -t "$SESSION" 2>/dev/null && echo "  Killed tmux session: $SESSION" || true
pkill -9 -f "move_group"        2>/dev/null && echo "  Killed: move_group"        || true
pkill -9 -f "ros2_control_node" 2>/dev/null && echo "  Killed: ros2_control_node" || true
pkill -9 -f "robot_agent"       2>/dev/null && echo "  Killed: robot_agent"       || true
pkill -9 -f "vllm serve"        2>/dev/null && echo "  Killed: vllm"              || true
pkill -9 -f "Cosmos-Reason2"    2>/dev/null && echo "  Killed: Cosmos process"    || true
pkill -9 -f "EngineCore"        2>/dev/null && echo "  Killed: vLLM EngineCore"   || true
pkill -9 -f "ollama serve"      2>/dev/null && echo "  Killed: ollama serve"      || true

# Force-release any remaining GPU memory from stale processes
for pid in $(sudo fuser /dev/nvidia0 2>/dev/null); do
    if [ "$pid" != "1328" ]; then  # skip Xorg
        sudo kill -9 "$pid" 2>/dev/null && echo "  Killed GPU process: $pid" || true
    fi
done

bash -c "source $ROS_SETUP 2>/dev/null; ros2 daemon stop 2>/dev/null; ros2 daemon start 2>/dev/null" || true

sleep 2
echo "[launch_all] Cleanup done."

# ---------------------------------------------------------------------------
# Helper: run a command in a new tmux window under bash
# ---------------------------------------------------------------------------
new_window() {
    local name="$1"
    local cmd="$2"
    tmux new-window -t "$SESSION" -n "$name"
    # Keep the window alive even if the command exits (shows the error)
    tmux send-keys -t "$SESSION:$name" \
        "bash --rcfile <(echo '. $ROS_SETUP 2>/dev/null; . $WS_SETUP 2>/dev/null') -c $(printf '%q' "$cmd"); echo '[window: $name exited — press Enter to close]'; read" \
        Enter
}

# ---------------------------------------------------------------------------
# Create tmux session with window 0
# ---------------------------------------------------------------------------
tmux new-session -d -s "$SESSION" -n "bringup" -x 220 -y 50

# ---------------------------------------------------------------------------
# Window 0: bringup
# ---------------------------------------------------------------------------
tmux send-keys -t "$SESSION:bringup" \
    "bash -c 'source $ROS_SETUP && source $WS_SETUP && cd $WS && ros2 launch ur5e_robotiq_moveit_config bringup.launch.py; echo \"[bringup exited]\"; read'" \
    Enter

# ---------------------------------------------------------------------------
# Window 1: cosmos — start immediately, model load takes time
# ---------------------------------------------------------------------------
tmux new-window -t "$SESSION" -n "cosmos"
tmux send-keys -t "$SESSION:cosmos" \
    "bash -c 'export PATH=\"\$HOME/.local/bin:\$PATH\"; echo \"[cosmos] Starting vLLM with $COSMOS_MODEL...\"; vllm serve $COSMOS_MODEL --max-model-len 16384 --reasoning-parser qwen3 --port 8000 --gpu-memory-utilization 0.85; echo \"[cosmos exited]\"; read'" \
    Enter

# ---------------------------------------------------------------------------
# Window 2: ollama
# ---------------------------------------------------------------------------
tmux new-window -t "$SESSION" -n "ollama"
tmux send-keys -t "$SESSION:ollama" \
    "bash -c 'echo \"[ollama] Starting...\"; ollama serve; echo \"[ollama exited]\"; read'" \
    Enter

# ---------------------------------------------------------------------------
# Window 3: agent — waits for bringup (/joint_states) AND Cosmos (/v1/models)
# ---------------------------------------------------------------------------
tmux new-window -t "$SESSION" -n "agent"

AGENT_CMD="bash -c '
source $ROS_SETUP && source $WS_SETUP

echo \"[agent] Waiting for Cosmos vLLM to be ready (port 8000)...\"
until curl -sf http://localhost:8000/v1/models > /dev/null 2>&1; do
    echo \"[agent]   ... Cosmos not ready yet, retrying in 10s\"
    sleep 10
done
echo \"[agent] Cosmos is ready!\"

echo \"[agent] Waiting for ROS bringup (/joint_states)...\"
until ros2 topic list 2>/dev/null | grep -q joint_states; do
    echo \"[agent]   ... bringup not ready yet, retrying in 5s\"
    sleep 5
done
echo \"[agent] Bringup is ready!\"

echo \"[agent] Starting robot_agent...\"
ros2 run robot_agent robot_agent
echo \"[agent exited]\"; read
'"

tmux send-keys -t "$SESSION:agent" "$AGENT_CMD" Enter

# ---------------------------------------------------------------------------
# Focus bringup window and attach
# ---------------------------------------------------------------------------
tmux select-window -t "$SESSION:bringup"

echo ""
echo "======================================================"
echo "  Session '$SESSION' running — 4 windows:"
echo "  [0] bringup  ros2 launch bringup.launch.py"
echo "  [1] cosmos   vllm serve $COSMOS_MODEL"
echo "  [2] ollama   ollama serve"
echo "  [3] agent    waits for Cosmos + bringup, then starts"
echo "======================================================"
echo "  Switch windows : Ctrl+b <0-3>"
echo "  Detach session : Ctrl+b d"
echo "  Kill session   : tmux kill-session -t $SESSION"
echo ""
echo "  Once all services are up, run the pick-and-place:"
echo "    source $ROS_SETUP && source $WS_SETUP"
echo "    python3 $WS/tests/test_pick_place_box.py"
echo "======================================================"
echo ""

tmux attach-session -t "$SESSION"
