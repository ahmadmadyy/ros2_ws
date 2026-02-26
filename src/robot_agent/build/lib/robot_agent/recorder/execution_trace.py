from dataclasses import dataclass, field
from typing import List


@dataclass
class JointSnapshot:
    timestamp: float
    joint_names: List[str]
    positions: List[float]
    velocities: List[float]


@dataclass
class ExecutionTrace:
    trace_id: str
    label: str
    snapshots: List[JointSnapshot] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration_sec(self) -> float:
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return 0.0

    def to_trajectory_json(self, max_rows: int = 50) -> list:
        """Convert trace snapshots to a JSON-serialisable list for LLM consumption."""
        if not self.snapshots:
            return []
        step = max(1, len(self.snapshots) // max_rows)
        t0 = self.start_time
        rows = []
        for i in range(0, len(self.snapshots), step):
            snap = self.snapshots[i]
            rows.append({
                "t": round(snap.timestamp - t0, 4),
                "joints": {
                    name: round(pos, 6)
                    for name, pos in zip(snap.joint_names, snap.positions)
                },
            })
        return rows

    def to_summary_text(self, max_rows: int = 20) -> str:
        """Convert trace to a compact text summary for LLM consumption."""
        if not self.snapshots:
            return "Empty trace (no snapshots recorded)"

        lines = [f"Trace: {self.label} | Duration: {self.duration_sec:.2f}s | Snapshots: {len(self.snapshots)}"]
        lines.append(f"Joints: {', '.join(self.snapshots[0].joint_names)}")
        lines.append("Time(s) | Positions (rad)")
        lines.append("-" * 60)

        # Subsample to max_rows
        step = max(1, len(self.snapshots) // max_rows)
        t0 = self.start_time
        for i in range(0, len(self.snapshots), step):
            snap = self.snapshots[i]
            t = snap.timestamp - t0
            pos = ", ".join(f"{p:.4f}" for p in snap.positions)
            lines.append(f"{t:7.3f} | [{pos}]")

        return "\n".join(lines)
