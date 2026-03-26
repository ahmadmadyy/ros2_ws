# Cosmos-Reason2 Model Comparison: Open-Ended Trace Analysis
**Trace ID:** `569c9cf5`
**Models tested:** `nvidia/Cosmos-Reason2-2B` vs `nvidia/Cosmos-Reason2-8B`
**Prompt type:** Open-ended with minimal domain hint — models received raw joint data and only the context "this robot is performing a fastening operation"
**Trace duration:** 211.10 seconds | 20,225 snapshots @ 95.8 Hz

---

## Prompt Design

The previous test used a heavily annotated prompt that told the models exactly what the task was, labelled every joint with its function, and even named the strategy being used. That produced near-identical results — both models were just filling in a template.

This prompt strips all of that out. The models get:
- Hardware names only (UR arm + Robotiq 85)
- A plain list of joint names with no functional labels
- Raw snapshot data — positions, velocities, timestamps
- One minimal hint: **"this robot is performing a fastening operation"**
- Seven open-ended questions they have to answer from the data

The hint was added after a fully blind run showed neither model could cold-identify "screwing" from joint data alone. Rather than testing whether models can guess a task name, the goal here is to see how well they understand the mechanics once they know the domain.

---

## What's Actually in the Trace

For reference — ground truth:

The robot performs a **continuous top-down screwing operation**. The arm descends and the gripper closes at t=7.24s to grasp a screwdriver. `wrist_3` then spins clockwise continuously (accumulating negative radians) to drive the screw. When it hits ~-5.80 rad (its joint range limit), the robot triggers a **wrist reset**: shoulder_lift dips slightly to disengage, `wrist_3` reverses counterclockwise for ~8.4 seconds to unwind, then screwing resumes. This happens **five times**, evenly spaced at ~40-second intervals. Total reset overhead: 42.3s = 20% of runtime.

At t=202.20s the gripper opens, releasing the screwdriver. The arm returns home by t=211.10s.

The notable anomaly: at t=110.05s, `wrist_3` reports +18.5645 rad/s. Adjacent position samples show actual motion of ~1.5 rad/s — the reported value is a sensor artifact, not physical reality.

---

## 2B Response (7.0 seconds)

**Q1 — Task Identification:**
The 2B knew it was a fastening operation (it was told), but it could not identify which joint was responsible. It named `elbow_joint` as the primary driver of the fastening action, and described `wrist_3` as "fine-tuning the gripper's orientation." This is wrong — `wrist_3` is the screwing axis. The model had the data showing `wrist_3` accumulating several full radians of rotation in a single direction across hundreds of seconds, and still pointed at the elbow.

**Q3 — Joint Roles:**
The 2B described `wrist_3` as controlling "orientation and precision during the approach" — missing that it's spinning continuously to drive a screw. For the gripper, it said it "transitions from fully closed to open at t=110.05s" — a hallucination. The gripper does not change state at t=110.05s. That is where the velocity spike occurs. The model appears to have associated the anomalous reading with a gripper event.

**Q4 — Recurring Pattern:**
Got the count right this time — 5 cycles. However it described the pattern as involving `elbow_joint`, `shoulder_pan_joint` and `wrist_3` performing coordinated reorientation motions, with 20–25 second cycle durations. The actual pattern is `wrist_3` doing a CCW unwind for ~8.4 seconds every ~40 seconds. The model identified the right number but the wrong mechanism and wrong timing.

**Q5 — Anomaly:**
`CRITICAL_FAULT`. The model's own explanation says "likely a sensor artifact or numerical overflow" — then files it as a critical fault anyway. These are contradictory. A sensor artifact is a data pipeline issue, not a fault in the robot's execution.

**Gripper Events in JSON:**
Three entries: OPEN at t=110.05s, CLOSE at t=108.00s, CLOSE at t=114.09s — all fabricated. None of these are real state changes. The actual close at t=7.24s and the actual open at t=202.20s are both absent from the list.

**Verdict:** CONDITIONAL_PASS, 5.0/10

---

## 8B Response (25.0 seconds)

**Q1 — Task Identification:**
"Fastening operation using a Robotiq 85 gripper, likely securing a component via a screw or bolt. The fastening action is primarily driven by the `wrist_3_joint`... applying rotational force to secure the component." This is correct. The 8B identified the right joint, the right mechanism, and the right physical interpretation — rotational force applied through the terminal wrist axis with the gripper holding the tool. It also correctly identified `shoulder_lift` as the approach axis.

**Q3 — Joint Roles:**
`wrist_3` correctly described as "the primary actuator for the fastening action, applying rotational force." The recurring pattern of rotation between -5.8 and +6.0 rad is observed and noted. `shoulder_lift` correctly described as maintaining height in the -1.6 to -1.7 rad working range. Gripper correctly described as grasped throughout — though the prose says "remains closed for the duration," the JSON correctly captures the open at t=202.20s.

**Q4 — Recurring Pattern:**
Five cycles, wrist_3 cycling between -5.8 and +6.0 rad. Cycle duration estimated at ~20 seconds (off — the resets are 8.4s with ~40s screwing between them, but the 8B is describing the wrist's total travel arc rather than the reset duration specifically). The model describes these as "aligning and securing the fastener repeatedly" rather than joint limit recovery, which is still a misread of the mechanism — but it has the right joint, the right count, and the right approximate values.

**Q5 — Anomaly:**
`SENSOR_ARTIFACT`. "The velocity reading is not physically plausible and likely due to a sensor glitch or data corruption." Correct assessment, correct severity. It also notes "does not appear to disrupt the overall task execution" — accurate, since the robot continued normally after the spike.

**Gripper Events:**
Close at t=7.24s, open at t=202.20s. Both exact. Two events, correct types, correct timestamps.

**Verdict:** CONDITIONAL_PASS, 6.5/10

---

## Side-by-Side Comparison

| | Cosmos-Reason2-2B | Cosmos-Reason2-8B |
|---|---|---|
| Response time | 7.0s | 25.0s |
| Fastening joint identified | Elbow (wrong) | wrist_3 (correct) |
| Approach joint identified | shoulder_lift (correct) | shoulder_lift (correct) |
| wrist_3 role described correctly | No — "orientation adjustment" | Yes — "applying rotational force" |
| Recurring pattern count | 5 (correct) | 5 (correct) |
| Recurring pattern mechanism | Wrong — elbow/pan reorientation | Partially right — wrist cycling, wrong reason |
| Anomaly assessment | CRITICAL_FAULT (contradicts own explanation) | SENSOR_ARTIFACT (correct) |
| Gripper events — count | 3 (all fabricated) | 2 (correct) |
| Gripper close timestamp | Absent | t=7.24s (correct) |
| Gripper open timestamp | Absent | t=202.20s (correct) |
| Hallucinated gripper event at spike | Yes — OPEN at t=110.05s | No |
| Verdict | CONDITIONAL_PASS 5.0/10 | CONDITIONAL_PASS 6.5/10 |

---

## What the Results Show

**Task understanding — clear gap.** With just the hint "fastening operation," the 8B correctly identified `wrist_3` as the screwing axis and described it as applying rotational force. The 2B, given the same hint, still pointed at the elbow. This is the core question this test was designed to answer: does the model understand robot kinematics well enough to read joint data and say "that joint is spinning to drive a screw"? The 8B does. The 2B does not.

**The wrist reset mechanism — neither model fully gets it.** Both models identified 5 recurring cycles and both associated them with `wrist_3`. But neither explicitly explained the cycles as joint-limit recovery — a wrist that accumulates too much rotation in one direction and must unwind before continuing. The 8B is closer (it observes the -5.8 to +6.0 rad oscillation and connects it to the fastening action), but frames it as "re-alignment" rather than a physical constraint. This is the deepest understanding question in the test, and it's where both models fall short of a human analyst.

**Structured output reliability — still a 2B problem.** The 2B's gripper event list is completely fabricated — three entries around the velocity spike, none of which correspond to actual state changes. The actual close at t=7.24s and open at t=202.20s are missing. The 8B's two-entry list is exact. This pattern held across all test runs: the 8B reliably extracts structured data from traces, the 2B does not.

**Anomaly handling — consistent 8B advantage.** Across every run, the 8B correctly classified the velocity spike as a sensor artifact. Across every run, the 2B escalated it to CRITICAL_FAULT while its own prose said "probably a sensor error." This inconsistency between the model's reasoning and its severity classification is the 2B's most repeatable failure mode.

**Speed:** 7s vs 25s. The 2B is fast but producing unreliable outputs. For any workflow where the output feeds into a downstream system — anomaly triage, event extraction, quality scoring — the 8B is the only one producing data that can be trusted without manual review.
