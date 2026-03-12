from dataclasses import dataclass, field
from typing import List

# UR5e arm joints in canonical order (j1..j6 as referenced in prompts)
_ARM_JOINT_ORDER = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]
_GRIPPER_JOINT = "rq_robotiq_85_left_knuckle_joint"
_GRIPPER_MAX_RAD = 0.79  # fully closed


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
        """Convert trace snapshots to a JSON-serialisable list for LLM consumption.

        For UR5e traces outputs clean 6-DOF format matching prompt descriptions:
          {"t": <elapsed_s>, "joints": [j1..j6], "gripper_state": 0.0-1.0}
        Falls back to raw joint dict for non-UR5e traces.
        """
        if not self.snapshots:
            return []
        step = max(1, len(self.snapshots) // max_rows)
        t0 = self.start_time

        # Detect UR5e trace once
        first_names = set(self.snapshots[0].joint_names)
        is_ur5e = all(j in first_names for j in _ARM_JOINT_ORDER)

        rows = []
        for i in range(0, len(self.snapshots), step):
            snap = self.snapshots[i]
            t = round(snap.timestamp - t0, 2)

            if is_ur5e:
                name_to_pos = dict(zip(snap.joint_names, snap.positions))
                arm = [round(name_to_pos.get(j, 0.0), 4) for j in _ARM_JOINT_ORDER]
                gripper_raw = name_to_pos.get(_GRIPPER_JOINT, 0.0)
                gripper_state = round(min(1.0, max(0.0, gripper_raw / _GRIPPER_MAX_RAD)), 2)
                rows.append({"t": t, "joints": arm, "gripper_state": gripper_state})
            else:
                rows.append({
                    "t": t,
                    "joints": {
                        name: round(pos, 4)
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
