import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import rclpy
from rclpy.executors import MultiThreadedExecutor
import uvicorn

from .agent_node import AgentNode
from .app import create_app

_VLLM_HEALTH_URL = "http://localhost:8000/health"
_VLLM_READY_TIMEOUT = 300   # seconds to wait for vLLM to become ready
_VLLM_POLL_INTERVAL = 5     # seconds between health-check polls


def _is_vllm_running() -> bool:
    try:
        urllib.request.urlopen(_VLLM_HEALTH_URL, timeout=3)
        return True
    except Exception:
        return False


def _kill_existing_vllm():
    subprocess.run(
        ["pkill", "-9", "-f", "vllm serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def _start_vllm(model_size: str):
    """Launch vLLM in the background and block until /health responds."""
    model = f"nvidia/Cosmos-Reason2-{model_size}"
    vllm_bin = shutil.which("vllm") or os.path.expanduser("~/.local/bin/vllm")
    cmd = [
        vllm_bin, "serve", model,
        "--max-model-len", "16384",
        "--reasoning-parser", "qwen3",
        "--port", "8000",
        "--gpu-memory-utilization", "0.85",
    ]
    print(f"[robot_agent] Starting vLLM: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Stream vLLM output in background thread
    def _stream():
        for line in proc.stdout:
            print(f"[vllm] {line}", end="", flush=True)

    threading.Thread(target=_stream, daemon=True).start()

    # Block until vLLM is ready
    deadline = time.monotonic() + _VLLM_READY_TIMEOUT
    while time.monotonic() < deadline:
        if _is_vllm_running():
            print(f"[robot_agent] vLLM ready (model={model})", flush=True)
            return proc
        time.sleep(_VLLM_POLL_INTERVAL)
        print(f"[robot_agent] Waiting for vLLM ({model})...", flush=True)

    proc.kill()
    print("[robot_agent] ERROR: vLLM did not become ready within timeout. Aborting.", flush=True)
    sys.exit(1)


def main():
    # ----------------------------------------------------------------
    # Parse cosmos_size from ROS args before rclpy.init() so we can
    # start vLLM early. Look for --ros-args -p cosmos_size:=<val>.
    # ----------------------------------------------------------------
    cosmos_size = "2B"
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg.startswith("cosmos_size:="):
            cosmos_size = arg.split(":=", 1)[1].upper()
        elif arg == "-p" and i + 1 < len(args) and args[i + 1].startswith("cosmos_size:="):
            cosmos_size = args[i + 1].split(":=", 1)[1].upper()

    if cosmos_size not in ("2B", "8B"):
        print(f"[robot_agent] Unknown cosmos_size '{cosmos_size}', defaulting to 2B", flush=True)
        cosmos_size = "2B"

    # Kill any existing vLLM, then start the chosen model (blocks until ready)
    if _is_vllm_running():
        print("[robot_agent] Existing vLLM detected — stopping it...", flush=True)
        _kill_existing_vllm()

    _start_vllm(cosmos_size)

    # ----------------------------------------------------------------
    # Normal robot_agent startup
    # ----------------------------------------------------------------
    rclpy.init()
    node = AgentNode()

    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    ros_thread = threading.Thread(target=executor.spin, daemon=True)
    ros_thread.start()

    app = create_app(node)

    node.get_logger().info('Starting FastAPI server on http://0.0.0.0:8080')

    try:
        uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info('Shutting down...')
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
