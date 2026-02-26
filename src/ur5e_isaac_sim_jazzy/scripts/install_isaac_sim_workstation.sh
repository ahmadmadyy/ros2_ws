#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME}"
TARGET_DIR="${TARGET_DIR:-$HOME/isaacsim}"
ARCHIVE_PATH="${ARCHIVE_PATH:-}"
TMP_EXTRACT_DIR="$(mktemp -d /tmp/isaacsim_extract_XXXXXX)"

cleanup() {
  rm -rf "$TMP_EXTRACT_DIR"
}
trap cleanup EXIT

if [[ -z "$ARCHIVE_PATH" ]]; then
  cat <<'MSG'
[INFO] Download the Isaac Sim workstation Linux archive from:
  https://docs.isaacsim.omniverse.nvidia.com/latest/installation/download.html

Then run:
  ARCHIVE_PATH=/path/to/isaac-sim-*.(zip|tar.gz) \
  ros2 run ur5e_isaac_sim_jazzy install_isaac_sim_workstation.sh
MSG
  exit 0
fi

if [[ ! -f "$ARCHIVE_PATH" ]]; then
  echo "[ERROR] Archive not found: $ARCHIVE_PATH"
  exit 1
fi

mkdir -p "$INSTALL_DIR"
case "$ARCHIVE_PATH" in
  *.tar.gz|*.tgz)
    tar -xzf "$ARCHIVE_PATH" -C "$TMP_EXTRACT_DIR"
    ;;
  *.zip)
    unzip -q "$ARCHIVE_PATH" -d "$TMP_EXTRACT_DIR"
    ;;
  *)
    echo "[ERROR] Unsupported archive format: $ARCHIVE_PATH"
    echo "Use .zip or .tar.gz"
    exit 1
    ;;
esac

LAUNCHER_PATH="$(find "$TMP_EXTRACT_DIR" -maxdepth 4 -type f -name isaac-sim.sh | head -n 1 || true)"
if [[ -z "$LAUNCHER_PATH" ]]; then
  echo "[ERROR] Could not find isaac-sim.sh in extracted archive."
  exit 1
fi

EXTRACTED_ROOT="$(dirname "$LAUNCHER_PATH")"
mkdir -p "$(dirname "$TARGET_DIR")"
rm -rf "$TARGET_DIR"
mv "$EXTRACTED_ROOT" "$TARGET_DIR"

if [[ ! -x "$TARGET_DIR/isaac-sim.sh" ]]; then
  echo "[ERROR] Installation finished but launcher not found in $TARGET_DIR"
  exit 1
fi

echo "[OK] Isaac Sim installed at $TARGET_DIR"
echo "Next: export ISAAC_SIM_ROOT=$TARGET_DIR"
