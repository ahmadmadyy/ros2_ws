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

_VLLM_READY_TIMEOUT = 300   # seconds to wait for vLLM to become ready
_VLLM_POLL_INTERVAL = 5     # seconds between health-check polls
_MANAGE_VLLM_ENV = "ROBOT_AGENT_MANAGE_VLLM"

_VLLM_INSTANCES = [
    {"model": "nvidia/Cosmos-Reason2-2B", "port": 8000, "gpu_util": "0.40"},
    {"model": "nvidia/Cosmos-Reason2-8B", "port": 8001, "gpu_util": "0.55"},
]


def _is_vllm_running(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3)
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


def _launch_vllm_proc(model: str, port: int, gpu_util: str) -> subprocess.Popen:
    """Popen a vLLM server — returns immediately, does not wait for readiness."""
    vllm_bin = shutil.which("vllm") or os.path.expanduser("~/.local/bin/vllm")
    cmd = [
        vllm_bin, "serve", model,
        "--max-model-len", "16384",
        "--reasoning-parser", "qwen3",
        "--port", str(port),
        "--gpu-memory-utilization", gpu_util,
    ]
    print(f"[robot_agent] Launching vLLM: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def _stream():
        for line in proc.stdout:
            print(f"[vllm:{port}] {line}", end="", flush=True)

    threading.Thread(target=_stream, daemon=True).start()
    return proc


def _wait_for_vllm(model: str, port: int, proc: subprocess.Popen):
    """Block until the vLLM server on *port* passes its health check."""
    deadline = time.monotonic() + _VLLM_READY_TIMEOUT
    while time.monotonic() < deadline:
        if _is_vllm_running(port):
            print(f"[robot_agent] vLLM ready — model={model} port={port}", flush=True)
            return
        time.sleep(_VLLM_POLL_INTERVAL)
        print(f"[robot_agent] Waiting for vLLM ({model}, port {port})...", flush=True)

    proc.kill()
    print(f"[robot_agent] ERROR: vLLM ({model}, port {port}) did not become ready. Aborting.", flush=True)
    sys.exit(1)


def _start_both_vllm():
    """Launch both vLLM instances sequentially to avoid VRAM contention."""
    # Kill any stale processes first
    if any(_is_vllm_running(inst["port"]) for inst in _VLLM_INSTANCES):
        print("[robot_agent] Existing vLLM instance(s) detected — stopping them...", flush=True)
        _kill_existing_vllm()

    # Start 2B first, wait until fully ready, then start 8B.
    # Loading in parallel causes both to reserve VRAM simultaneously,
    # leaving the 2B with negative KV-cache budget on a 46 GB L40S.
    for inst in _VLLM_INSTANCES:
        proc = _launch_vllm_proc(inst["model"], inst["port"], inst["gpu_util"])
        _wait_for_vllm(inst["model"], inst["port"], proc)


def main():
    # By default the agent manages its own local vLLM instances. Test harnesses
    # such as tests/run_full_pipeline.py can disable this and provide external
    # Cosmos servers instead.
    manage_vllm = os.getenv(_MANAGE_VLLM_ENV, "1").strip().lower() not in {
        "0", "false", "no"
    }
    if manage_vllm:
        _start_both_vllm()
    else:
        print(
            f"[robot_agent] Skipping internal vLLM startup because "
            f"{_MANAGE_VLLM_ENV}={os.getenv(_MANAGE_VLLM_ENV)}",
            flush=True,
        )

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
