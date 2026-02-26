import threading
import uuid
from typing import Dict, Optional

from rclpy.node import Node
from sensor_msgs.msg import JointState

from .execution_trace import JointSnapshot, ExecutionTrace


class JointStateRecorder:
    """Records joint state traces during skill execution."""

    def __init__(self, node: Node):
        self._node = node
        self._lock = threading.Lock()
        self._recording = False
        self._current_trace: list = []
        self._label = ""
        self._trace_id = ""

        # Store completed traces
        self.traces: Dict[str, ExecutionTrace] = {}

    def start_recording(self, label: str = "") -> str:
        """Start recording. Returns the trace_id."""
        with self._lock:
            self._trace_id = str(uuid.uuid4())[:8]
            self._label = label
            self._current_trace = []
            self._recording = True
            self._node.get_logger().info(f'Recording started: {self._trace_id} ({label})')
            return self._trace_id

    def stop_recording(self) -> Optional[ExecutionTrace]:
        """Stop recording and return the completed trace."""
        with self._lock:
            if not self._recording:
                return None
            self._recording = False

            start_time = self._current_trace[0].timestamp if self._current_trace else 0.0
            end_time = self._current_trace[-1].timestamp if self._current_trace else 0.0

            trace = ExecutionTrace(
                trace_id=self._trace_id,
                label=self._label,
                snapshots=list(self._current_trace),
                start_time=start_time,
                end_time=end_time,
            )
            self.traces[self._trace_id] = trace
            self._node.get_logger().info(
                f'Recording stopped: {self._trace_id} ({len(trace.snapshots)} snapshots, {trace.duration_sec:.2f}s)'
            )
            return trace

    def on_joint_state(self, msg: JointState):
        """Called from AgentNode's joint_state callback."""
        with self._lock:
            if not self._recording:
                return
            timestamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
            self._current_trace.append(JointSnapshot(
                timestamp=timestamp,
                joint_names=list(msg.name),
                positions=list(msg.position),
                velocities=list(msg.velocity) if msg.velocity else [],
            ))

    def get_trace(self, trace_id: str) -> Optional[ExecutionTrace]:
        return self.traces.get(trace_id)

    def list_traces(self) -> list:
        return [
            {
                "trace_id": t.trace_id,
                "label": t.label,
                "duration_sec": round(t.duration_sec, 3),
                "num_snapshots": len(t.snapshots),
            }
            for t in self.traces.values()
        ]
