#!/usr/bin/env bash
set -euo pipefail

ISAAC_SIM_ROOT_DEFAULT="$HOME/isaacsim"
ISAAC_SIM_ROOT="${ISAAC_SIM_ROOT:-$ISAAC_SIM_ROOT_DEFAULT}"

if [[ ! -x "$ISAAC_SIM_ROOT/isaac-sim.sh" && -x "$HOME/isaac-sim.sh" ]]; then
  ISAAC_SIM_ROOT="$HOME"
fi

if [[ ! -x "$ISAAC_SIM_ROOT/isaac-sim.sh" ]]; then
  echo "[ERROR] Isaac Sim launcher not found: $ISAAC_SIM_ROOT/isaac-sim.sh"
  echo "Set ISAAC_SIM_ROOT to your install folder, e.g.:"
  echo "  export ISAAC_SIM_ROOT=$HOME/isaacsim"
  exit 1
fi

if [[ -f "/opt/ros/jazzy/setup.bash" ]]; then
  # Isaac Sim docs recommend sourcing your system ROS 2 install for Jazzy.
  # setup.bash is not nounset-safe, so temporarily disable it.
  # shellcheck disable=SC1091
  set +u
  source /opt/ros/jazzy/setup.bash
  set -u
else
  echo "[WARN] /opt/ros/jazzy/setup.bash not found. Continuing without system ROS sourcing."
fi

if [[ -n "${ROS2_WS_SETUP:-}" && -f "${ROS2_WS_SETUP}" ]]; then
  # Optional overlay for your local workspace.
  # shellcheck disable=SC1090
  set +u
  source "${ROS2_WS_SETUP}"
  set -u
fi

export RMW_IMPLEMENTATION="${RMW_IMPLEMENTATION:-rmw_fastrtps_cpp}"

ISAAC_STAGE_PATH="${ISAAC_STAGE_PATH:-}"
ISAAC_EXTRA_ARGS="${ISAAC_EXTRA_ARGS:-}"

cmd=("$ISAAC_SIM_ROOT/isaac-sim.sh")

if [[ -n "$ISAAC_STAGE_PATH" ]]; then
  if [[ ! -f "$ISAAC_STAGE_PATH" ]]; then
    echo "[ERROR] ISAAC_STAGE_PATH does not exist: $ISAAC_STAGE_PATH"
    exit 1
  fi
  echo "[INFO] Autoloading Isaac stage: $ISAAC_STAGE_PATH"
  cmd+=("$ISAAC_STAGE_PATH")
fi

if [[ -n "$ISAAC_EXTRA_ARGS" ]]; then
  # Optional extra args, e.g. '--/app/window/width=1920 --/app/window/height=1080'
  # shellcheck disable=SC2206
  extra_args=($ISAAC_EXTRA_ARGS)
  cmd+=("${extra_args[@]}")
fi

exec "${cmd[@]}"
