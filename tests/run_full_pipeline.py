#!/usr/bin/env python3
"""
Full automated pipeline — one command to run everything.

Steps:
  0.  Kill all running processes
  1.  Start bringup, Cosmos 2B (port 8000), Cosmos 8B (port 8001),
      robot_agent (port 8080) — all in a tmux session
  2.  Wait for every service to be ready
  3.  Execute test_screw_continuous_cosmos.py  → new trace recorded
  4.  Run run_8b_pipeline.py on the new trace  → improved waypoints generated
  5.  Execute execute_cosmos_waypoints.py       → new cosmos-executed trace recorded
  6.  Run run_8b_pipeline.py on the cosmos trace → final evaluation
  7.  Print score comparison (step 4 vs step 6)

Usage (one command):
  source /opt/ros/jazzy/setup.bash && source ~/ros2_ws/install/setup.bash
  python3 ~/ros2_ws/tests/run_full_pipeline.py

GPU note (L40S 46 GB):
  Load order matters!  8B starts first (alone), 2B starts only after 8B is ready.
  This avoids the race condition where both compete for GPU memory simultaneously.

  Cosmos 8B  --gpu-memory-utilization 0.58  → started first, ~27 GB (model + KV cache)
  Cosmos 2B  --gpu-memory-utilization 0.38  → started after 8B is loaded
                                               vLLM v1 check: free_mem >= util×total
                                               free after 8B ≈ 18.4 GB; 0.38×44.4=16.9 GB ✓
  Total ≈ 44.5 GB on L40S 46 GB.  Both models respond to inference.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WS             = Path(__file__).parent.parent
SCREW_TRACE    = WS / "outputs" / "screw_trace"
WAYPOINTS_DIR  = WS / "outputs" / "cosmos" / "8b"
EVAL_DIR       = WS / "outputs" / "eval"   / "8b"

# ---------------------------------------------------------------------------
# Service URLs / models
# ---------------------------------------------------------------------------
COSMOS_2B_URL   = "http://localhost:8000"
COSMOS_8B_URL   = "http://localhost:8001"
AGENT_URL       = "http://localhost:8080"
COSMOS_2B_MODEL = "nvidia/Cosmos-Reason2-2B"
COSMOS_8B_MODEL = "nvidia/Cosmos-Reason2-8B"

# ---------------------------------------------------------------------------
# vLLM GPU memory settings  (L40S 46 GB)
#
# IMPORTANT — load order: 8B starts first (alone), 2B starts only after 8B
# is confirmed ready.  When 2B starts it sees ~18.4 GB free.
# vLLM v1 check: free_memory >= gpu_util × total_memory
#   → gpu_util must be ≤ 18.4 / 44.4 = 0.414.  We use 0.38 for safety.
# ---------------------------------------------------------------------------
GPU_MEM_2B = "0.38"   # started AFTER 8B; vLLM v1 check: free_mem >= util×total
GPU_MEM_8B = "0.58"   # started first (alone); ~27 GB model + KV cache

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------
COSMOS_READY_TIMEOUT = 600   # vLLM model loading can take ~5–10 min
AGENT_READY_TIMEOUT  = 90

# ---------------------------------------------------------------------------
# tmux session name
# ---------------------------------------------------------------------------
TMUX = "robot_pipeline"

# ---------------------------------------------------------------------------
# ROS source command — used both in tmux panes and bash -c subprocesses
# ---------------------------------------------------------------------------
ROS_SOURCE = (
    "source /opt/ros/jazzy/setup.bash && "
    "source ~/ros2_ws/install/setup.bash"
)
ROBOT_AGENT_SOURCE = WS / "src" / "robot_agent"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    bar = "=" * 66
    print(f"\n{bar}\n  {title}\n{bar}")


def sh(cmd: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a shell command, echo it, return the result."""
    print(f"  $ {cmd}")
    return subprocess.run(cmd, shell=True, check=check, **kwargs)


def tmux_send(pane: str, cmd: str) -> None:
    """Send a command to a named tmux window in TMUX session."""
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{TMUX}:{pane}", cmd, "Enter"],
        check=False,
    )


def tmux_new_window(name: str) -> None:
    subprocess.run(
        ["tmux", "new-window", "-t", TMUX, "-n", name],
        check=False,
    )
    time.sleep(0.4)


# ---------------------------------------------------------------------------
# Service readiness polling
# ---------------------------------------------------------------------------

async def _poll_vllm(url: str, model: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{url}/v1/models", timeout=5.0)
                if r.status_code == 200:
                    served = [m["id"] for m in r.json().get("data", [])]
                    if any(model in s for s in served):
                        return True
            except Exception:
                pass
            elapsed = int(timeout - (deadline - time.monotonic()))
            print(f"    [{elapsed:>4}s] waiting for {url} …", end="\r", flush=True)
            await asyncio.sleep(5)
    print()
    return False


async def _poll_agent(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{url}/api/v1/health", timeout=5.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(3)
    return False


# ---------------------------------------------------------------------------
# STEP 0 — Kill everything
# ---------------------------------------------------------------------------

def step0_kill_all() -> None:
    section("STEP 0 — Stopping all running processes")
    cmds = [
        f"tmux kill-session -t {TMUX} 2>/dev/null || true",
        "tmux kill-session -t robot_stack 2>/dev/null || true",
        "pkill -9 -f 'bringup.launch.py' 2>/dev/null || true",
        "pkill -9 -f 'ros2 launch ur5e_robotiq_moveit_config bringup.launch.py' 2>/dev/null || true",
        "pkill -9 -f 'move_group'        2>/dev/null || true",
        "pkill -9 -f 'robot_state_publisher' 2>/dev/null || true",
        "pkill -9 -f 'controller_manager' 2>/dev/null || true",
        "pkill -9 -f 'spawner' 2>/dev/null || true",
        "pkill -9 -f 'ros2_control_node' 2>/dev/null || true",
        "pkill -9 -f 'robot_agent'       2>/dev/null || true",
        "pkill -9 -f 'vllm serve'        2>/dev/null || true",
        "pkill -9 -f 'Cosmos-Reason2'    2>/dev/null || true",
        "pkill -9 -f 'EngineCore'        2>/dev/null || true",
        "pkill -9 -f 'ollama serve'      2>/dev/null || true",
        f"bash -c '{ROS_SOURCE} && ros2 daemon stop 2>/dev/null || true'",
        "sleep 3",
        f"bash -c '{ROS_SOURCE} && ros2 daemon start'",
        "sleep 2",
    ]
    for cmd in cmds:
        sh(cmd, check=False)
    print("  [OK] All processes stopped.")


# ---------------------------------------------------------------------------
# STEP 1 — Start services
# ---------------------------------------------------------------------------

def step1_start_services() -> None:
    """Start bringup, Cosmos 8B, and agent.

    Cosmos 2B is intentionally NOT started here.  It will be started in
    step2_wait_for_services() only after Cosmos 8B is confirmed ready — this
    avoids the GPU memory race that crashes 2B when both launch simultaneously.
    """
    section("STEP 1 — Starting services in tmux session")

    # Create session with the bringup window
    sh(f"tmux new-session -d -s {TMUX} -n bringup", check=False)
    time.sleep(0.5)

    # ── Window: bringup ──────────────────────────────────────────────────
    tmux_send(
        "bringup",
        f"{ROS_SOURCE} && ros2 launch ur5e_robotiq_moveit_config bringup.launch.py",
    )
    print(f"  [bringup]  tmux window 'bringup' started")

    # ── Window: cosmos8b  (started FIRST — gets full GPU) ────────────────
    tmux_new_window("cosmos8b")
    tmux_send(
        "cosmos8b",
        (
            f'export PATH="$HOME/.local/bin:$PATH" && '
            f"vllm serve {COSMOS_8B_MODEL} "
            f"--max-model-len 16384 "
            f"--reasoning-parser qwen3 "
            f"--port 8001 "
            f"--gpu-memory-utilization {GPU_MEM_8B}"
        ),
    )
    print(f"  [cosmos8b] tmux window 'cosmos8b' started  (port 8001, gpu={GPU_MEM_8B})")

    # ── Window: cosmos2b  (placeholder — launched after 8B is ready) ─────
    tmux_new_window("cosmos2b")
    print(f"  [cosmos2b] tmux window 'cosmos2b' created  (will start after 8B is ready)")

    # ── Window: agent ────────────────────────────────────────────────────
    tmux_new_window("agent")
    tmux_send(
        "agent",
        (
            f'export ROBOT_AGENT_MANAGE_VLLM=0 && '
            f'export PYTHONPATH="{ROBOT_AGENT_SOURCE}:$PYTHONPATH" && '
            f"{ROS_SOURCE} && python3 -m robot_agent.main"
        ),
    )
    print(f"  [agent]    tmux window 'agent' started     (port 8080)")

    print(f"\n  Attach to watch: tmux attach -t {TMUX}")


# ---------------------------------------------------------------------------
# STEP 2 — Wait for all services
# ---------------------------------------------------------------------------

async def step2_wait_for_services() -> None:
    """Wait for 8B, then start 2B, then wait for 2B + agent.

    Sequential order prevents the GPU memory race:
      8B loads alone  →  claims ~27 GB
      2B loads after  →  uses remaining ~9 GB (model + small KV cache)
    """
    section("STEP 2 — Waiting for services to become ready")

    # ── 8B first ────────────────────────────────────────────────────────
    print(f"\n  [1/3] Cosmos 8B  ({COSMOS_8B_URL})  model={COSMOS_8B_MODEL}")
    ok = await _poll_vllm(COSMOS_8B_URL, COSMOS_8B_MODEL, COSMOS_READY_TIMEOUT)
    if not ok:
        print(f"\n  ERROR: Cosmos 8B not ready after {COSMOS_READY_TIMEOUT}s — aborting.")
        sys.exit(1)
    print(f"\n  [OK] Cosmos 8B ready  (GPU footprint locked)")

    # ── Now launch 2B — 8B memory is committed, 2B sees stable free RAM ─
    print(f"\n  Starting Cosmos 2B now (8B is loaded, GPU state is stable) …")
    tmux_send(
        "cosmos2b",
        (
            f'export PATH="$HOME/.local/bin:$PATH" && '
            f"vllm serve {COSMOS_2B_MODEL} "
            f"--max-model-len 16384 "
            f"--reasoning-parser qwen3 "
            f"--port 8000 "
            f"--gpu-memory-utilization {GPU_MEM_2B}"
        ),
    )
    print(f"  [cosmos2b] launched  (port 8000, gpu={GPU_MEM_2B})")

    # ── Wait for 2B ──────────────────────────────────────────────────────
    print(f"\n  [2/3] Cosmos 2B  ({COSMOS_2B_URL})  model={COSMOS_2B_MODEL}")
    ok = await _poll_vllm(COSMOS_2B_URL, COSMOS_2B_MODEL, COSMOS_READY_TIMEOUT)
    if not ok:
        print(f"\n  ERROR: Cosmos 2B not ready after {COSMOS_READY_TIMEOUT}s — aborting.")
        sys.exit(1)
    print(f"\n  [OK] Cosmos 2B ready")

    # ── Agent ────────────────────────────────────────────────────────────
    print(f"\n  [3/3] Robot agent  ({AGENT_URL})")
    ok = await _poll_agent(AGENT_URL, AGENT_READY_TIMEOUT)
    if not ok:
        print(f"\n  ERROR: Agent not ready after {AGENT_READY_TIMEOUT}s — aborting.")
        sys.exit(1)
    print(f"  [OK] Robot agent ready")


# ---------------------------------------------------------------------------
# STEP 3 — Run test_screw_continuous_cosmos.py → new trace
# ---------------------------------------------------------------------------

def step3_run_screw_test() -> Path:
    section("STEP 3 — Executing test_screw_continuous_cosmos.py (recording trace)")

    before = set(SCREW_TRACE.glob("screw_continuous_*.json")) if SCREW_TRACE.exists() else set()

    result = sh(
        f"bash -c '{ROS_SOURCE} && python3 {WS}/tests/test_screw_continuous_cosmos.py'",
        check=False,
    )
    if result.returncode != 0:
        print(f"  ERROR: test_screw_continuous_cosmos.py exited {result.returncode} — aborting pipeline.")
        sys.exit(result.returncode)

    after = set(SCREW_TRACE.glob("screw_continuous_*.json")) if SCREW_TRACE.exists() else set()
    new = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)

    if new:
        trace = new[0]
    else:
        # Fallback: most recent file
        candidates = sorted(
            SCREW_TRACE.glob("screw_continuous_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print("  ERROR: No screw_continuous trace found — aborting.")
            sys.exit(1)
        trace = candidates[0]
        print("  WARNING: Could not detect a new trace; using most recent existing trace.")

    print(f"\n  New trace recorded: {trace.name}")
    return trace


# ---------------------------------------------------------------------------
# STEP 4 / 6 — Run 8B pipeline on a trace
# ---------------------------------------------------------------------------

def _run_8b_pipeline(trace: Path, label: str) -> tuple["Path | None", "float | None"]:
    """
    Run run_8b_pipeline.py on *trace*.
    Returns (waypoints_path, best_overall_score).
    waypoints_path is None on step 6 (re-evaluation — no new waypoints expected).
    """
    before_wp = set(WAYPOINTS_DIR.glob("waypoints_*.json")) if WAYPOINTS_DIR.exists() else set()
    before_ev = set(EVAL_DIR.glob("eval_*.json"))           if EVAL_DIR.exists()   else set()

    result = sh(
        f"python3 {WS}/tests/run_8b_pipeline.py --trace {trace}",
        check=False,
    )
    if result.returncode != 0:
        print(f"  WARNING: run_8b_pipeline.py exited {result.returncode}")

    # ── Find newest waypoints file ───────────────────────────────────────
    after_wp = set(WAYPOINTS_DIR.glob("waypoints_*.json")) if WAYPOINTS_DIR.exists() else set()
    new_wp   = sorted(after_wp - before_wp, key=lambda p: p.stat().st_mtime, reverse=True)
    waypoints_path = new_wp[0] if new_wp else None

    # ── Find best score from new eval files ─────────────────────────────
    after_ev = set(EVAL_DIR.glob("eval_*.json")) if EVAL_DIR.exists() else set()
    new_ev   = sorted(
        after_ev - before_ev,
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    best_score: float | None = None
    for ev_file in new_ev:
        try:
            data  = json.loads(ev_file.read_text())
            score = data.get("overall_score")
            if isinstance(score, (int, float)):
                if best_score is None or score > best_score:
                    best_score = score
        except Exception:
            pass

    if waypoints_path:
        print(f"\n  Waypoints generated: {waypoints_path.name}")
    if best_score is not None:
        print(f"  Best overall_score:  {best_score:.2f}/5.0")

    return waypoints_path, best_score


def step4_run_initial_pipeline(trace: Path) -> tuple["Path | None", "float | None"]:
    section("STEP 4 — 8B pipeline on initial trace  (2B analysis → 8B eval → waypoints)")
    return _run_8b_pipeline(trace, "initial")


def step6_run_final_eval(trace: Path) -> tuple["Path | None", "float | None"]:
    section("STEP 6 — 8B pipeline re-evaluation on cosmos-executed trace")
    return _run_8b_pipeline(trace, "cosmos-executed")


# ---------------------------------------------------------------------------
# STEP 5 — Execute cosmos waypoints → record new trace
# ---------------------------------------------------------------------------

def step5_execute_waypoints(waypoints: Path) -> Path:
    section("STEP 5 — Executing cosmos waypoints on robot (recording trace)")

    before = set(SCREW_TRACE.glob("cosmos_executed_*.json")) if SCREW_TRACE.exists() else set()

    result = sh(
        f"bash -c '{ROS_SOURCE} && "
        f"python3 {WS}/tests/execute_cosmos_waypoints.py {waypoints}'",
        check=False,
    )
    if result.returncode != 0:
        print(f"  WARNING: execute_cosmos_waypoints.py exited {result.returncode}")

    after = set(SCREW_TRACE.glob("cosmos_executed_*.json")) if SCREW_TRACE.exists() else set()
    new   = sorted(after - before, key=lambda p: p.stat().st_mtime, reverse=True)

    if new:
        trace = new[0]
    else:
        candidates = sorted(
            SCREW_TRACE.glob("cosmos_executed_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            print("  ERROR: No cosmos_executed trace found — aborting.")
            sys.exit(1)
        trace = candidates[0]
        print("  WARNING: Could not detect a new cosmos trace; using most recent.")

    print(f"\n  Cosmos-executed trace: {trace.name}")
    return trace


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def async_main() -> None:
    t_start = time.monotonic()

    section("FULL AUTOMATED PIPELINE")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Workspace: {WS}")

    # ── 0. Kill all ──────────────────────────────────────────────────────
    step0_kill_all()

    # ── 1. Start services ────────────────────────────────────────────────
    step1_start_services()

    # ── 2. Wait for services ─────────────────────────────────────────────
    await step2_wait_for_services()

    # ── 3. Record initial trace ──────────────────────────────────────────
    initial_trace = step3_run_screw_test()

    # ── 4. 8B pipeline → waypoints ───────────────────────────────────────
    waypoints, initial_score = step4_run_initial_pipeline(initial_trace)

    if waypoints is None:
        print("\n  ERROR: No waypoints file produced — cannot continue to step 5.")
        print(f"  Initial score: {initial_score}")
        _print_summary(initial_trace, None, None, initial_score, None, t_start)
        sys.exit(1)

    # ── 5. Execute waypoints → cosmos trace ──────────────────────────────
    cosmos_trace = step5_execute_waypoints(waypoints)

    # ── 6. Re-evaluate cosmos trace ──────────────────────────────────────
    _, final_score = step6_run_final_eval(cosmos_trace)

    # ── 7. Final report ──────────────────────────────────────────────────
    _print_summary(initial_trace, waypoints, cosmos_trace, initial_score, final_score, t_start)


def _print_summary(
    initial_trace: "Path | None",
    waypoints: "Path | None",
    cosmos_trace: "Path | None",
    initial_score: "float | None",
    final_score: "float | None",
    t_start: float,
) -> None:
    section("FINAL RESULTS")
    elapsed = time.monotonic() - t_start

    def _fmt(v: "float | None") -> str:
        return f"{v:.2f}/5.0" if isinstance(v, float) else "N/A"

    print(f"  Initial score (original trace)   : {_fmt(initial_score)}")
    print(f"  Final score   (cosmos waypoints) : {_fmt(final_score)}")

    if isinstance(initial_score, float) and isinstance(final_score, float):
        delta = final_score - initial_score
        if delta > 0:
            print(f"  Improvement  : +{delta:.2f}  ✓  Score went up!")
        elif delta == 0:
            print(f"  No change    :  {delta:.2f}")
        else:
            print(f"  Regression   :  {delta:.2f}  ✗  Score decreased.")

    print()
    if initial_trace:
        print(f"  Original trace     : {initial_trace}")
    if waypoints:
        print(f"  Waypoints file     : {waypoints}")
    if cosmos_trace:
        print(f"  Cosmos exec trace  : {cosmos_trace}")

    print(f"\n  Total elapsed: {elapsed / 60:.1f} minutes")
    print("=" * 66 + "\n")

    print("  To re-attach to the running services:")
    print(f"    tmux attach -t {TMUX}")
    print()
    print("  To kill services when done:")
    print(f"    tmux kill-session -t {TMUX}")
    print()


def main() -> None:
    # httpx is required for service polling
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("ERROR: httpx is not installed.  Run:  pip install httpx")
        sys.exit(1)

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
