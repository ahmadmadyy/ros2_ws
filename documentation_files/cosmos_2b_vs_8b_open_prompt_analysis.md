# Cosmos-Reason2 Model Comparison: Open-Ended Trace Analysis
**Trace ID:** `569c9cf5` — unlabeled at inference time
**Models tested:** `nvidia/Cosmos-Reason2-2B` vs `nvidia/Cosmos-Reason2-8B`
**Prompt type:** Open-ended — models received raw joint data with no task label, no joint annotations, no hints about what the robot was doing
**Trace duration:** 211.10 seconds | 20,225 snapshots @ 95.8 Hz

---

## Why the Prompt Changed

The previous prompt explicitly told the models they were analyzing a "screwing task," annotated every joint with its purpose (e.g., "← primary screwing axis"), provided the coordinate convention, and even named the strategy being used. Under those conditions both models were essentially filling in a template — the prompt did most of the thinking for them, which is why their scores and verdicts were nearly identical.

The new prompt strips all of that out. The models receive:
- Hardware names only (UR arm, Robotiq 85 gripper)
- A plain list of joint names with no functional descriptions
- Raw snapshot data with timestamps, positions, and velocities
- Seven open-ended questions that require the model to derive meaning from the data itself

The goal is to measure whether the models actually understand robot motion, or whether they were just pattern-matching against a heavily annotated prompt.

---

## What's in the Trace (Ground Truth)

For reference, here is what was actually happening:

The robot performed a continuous top-down **screwing task**. Starting from home position, the arm descends and the gripper closes at t=7.24s to grasp a screwdriver. `wrist_3` then spins continuously clockwise (negative direction) to drive the screw. Because `wrist_3` has a physical joint range limit, it cannot accumulate rotation indefinitely — when it reaches approximately -5.80 rad, the robot triggers a **wrist reset**: it disengages slightly (shoulder_lift dips deeper), reverses `wrist_3` counterclockwise for ~8.4 seconds to unwind, then resumes screwing. This happens five times, evenly spaced at ~40-second intervals. The 20% of runtime spent in resets is a known overhead of this strategy.

At t=202.20s the gripper opens, releasing the screwdriver. The arm retracts to home by t=211.10s.

The notable anomaly: at t=110.05s during reset 3, `wrist_3` reports a velocity of +18.5645 rad/s. The positional delta between adjacent samples (~1.5 rad/s) makes this physically impossible — it is a sensor artifact, likely a numerical derivative glitch at the moment the velocity transitions from high positive to lower values.

---

## 2B Response (10.6 seconds)

**Q1 — Task Identification:** "Precise positioning or calibration task"

The 2B did not identify screwing. It described the task in vague terms — "reorienting the gripper and adjusting the arm's base" — without connecting the sustained `wrist_3` rotation, the arm descending toward a workpiece, or the gripper closure to any specific manipulation task. Positioning/calibration is a reasonable guess for an arm that mostly holds still, but this trace has a continuous spinning joint and a tool grip that together are the signature of fastening work.

**Q3 — Joint Roles:** The 2B described `wrist_3` as performing "fine-tuned adjustments to the gripper's orientation." It missed the core behavior entirely: `wrist_3` is spinning continuously in one direction for most of the trace, reversing periodically, accumulating several full revolutions. That is not "orientation adjustment."

**Q4 — Recurring Pattern:** The 2B identified a pattern repeating with a "2–3 second cycle" and reported `recurring_pattern_count: 20225` — the total number of snapshots in the trace. It counted every data point as a pattern occurrence. The actual pattern (five wrist resets, each lasting ~8.4 seconds, triggered every ~40 seconds) was completely missed.

**Q5 — Anomaly:** Classified as `CRITICAL_FAULT`. The reasoning was thin — "beyond typical range, likely a sensor artifact or numerical overflow error" — but then assigned CRITICAL severity despite calling it a sensor artifact. These two conclusions contradict each other. A sensor artifact is not a critical fault.

**Q6 — Gripper Events in JSON:** Listed 23 separate gripper events — one for every snapshot in the prompt, all labeled "CLOSE." The model did not understand that an event means a state *change*. It logged the gripper value for every snapshot rather than identifying the two actual transitions (close at t=7.24s, open at t=202.20s). This is a fundamental misread of the data.

**Verdict:** CONDITIONAL_PASS, 6.0/10

---

## 8B Response (25.9 seconds)

**Q1 — Task Identification:** "Pick-and-place task with a gripper"

The 8B also did not identify screwing, but its answer is more grounded in the data. It noticed the gripper closed at t=7.24s and stayed closed for most of the trace, the arm moved to a stable working height, and there were recurring vertical motions. Pick-and-place is wrong, but the model arrived at it through actual observation of the data — it saw the gripper close, saw the arm positioned consistently, and inferred object transport. That is better reasoning than the 2B's "calibration" guess.

**Q3 — Joint Roles:** The 8B described `wrist_3` as "moving from approximately -5.79 rad to +5.98 rad in cycles," which correctly identifies the reset pattern as a positional cycle. However, it interpreted this as the robot "positioning the gripper vertically" — confusing the rotational axis with the lift axis. The recurring wrist oscillation between -5.8 and +6.0 rad is the joint resetting after clockwise accumulation, not vertical positioning. The interpretation is wrong but the observation is right.

**Q4 — Recurring Pattern:** Correctly identified **5 cycles**. The cycle duration estimate (12–14 seconds) is off — the resets themselves last ~8.4s and are spaced ~40s apart — but the count is right. This is a meaningful difference from the 2B's 20,225-cycle answer.

**Q5 — Anomaly:** Classified as `SENSOR_ARTIFACT`. The explanation ("likely due to sensor error or data rounding, not aligned with mechanical constraints") is accurate. Assigning SENSOR_ARTIFACT (not CRITICAL_FAULT) for a value that the model itself noted is inconsistent with adjacent position data is the correct call.

**Q6 — Gripper Events:** **Two events only**: close at t=7.24s, open at t=202.20s. Both correct. The model understood that gripper events mean state changes, not snapshot values.

One internal inconsistency: in the prose for Q3, the 8B stated "there is no opening or closing observed after t=7.24s," but then correctly listed the open at t=202.20s in the JSON. The narrative contradicted the data-accurate JSON.

**Verdict:** CONDITIONAL_PASS, 6.5/10

---

## Side-by-Side Comparison

| | Cosmos-Reason2-2B | Cosmos-Reason2-8B |
|---|---|---|
| Response time | 10.6s | 25.9s |
| Task identified correctly | No — "calibration task" | No — "pick-and-place" |
| Task reasoning quality | Vague, no data support | Grounded in data observations |
| Recurring pattern count | 20,225 (every snapshot) | 5 (correct) |
| Recurring pattern duration | "2–3 seconds" | "12–14 seconds" (off but ballpark) |
| wrist_3 role understood | No — "fine adjustments" | Partially — saw the cycling but misread axis |
| Anomaly assessment | CRITICAL_FAULT (contradicts own explanation) | SENSOR_ARTIFACT (correct) |
| Gripper event count | 23 (every snapshot) | 2 (correct) |
| Gripper timestamps | All wrong | t=7.24s close, t=202.20s open — exact |
| Internal consistency | Low | Moderate (one prose/JSON contradiction) |

---

## What the Open Prompt Revealed

**Neither model identified the task as screwing.** This is the most important result. Without the task label in the prompt, both models defaulted to generic interpretations — calibration, pick-and-place — that don't account for the sustained unidirectional `wrist_3` rotation that is the signature of a fastening operation. Cosmos has been exposed to robot data, but recognizing "continuously spinning terminal wrist joint + gripper grip + downward approach = fastening" without explicit prompting is something neither model demonstrated here.

**The gap between 2B and 8B is clear on structured outputs.** The 2B's gripper event list (23 entries, all labeled CLOSE) shows the model doesn't distinguish between reading a value and detecting a change. The 8B's two-entry list with correct timestamps and event types is exactly what was asked for. This is not a marginal difference — the 2B's output would be completely unusable downstream.

**The recurring pattern question was the sharpest differentiator.** "20,225 occurrences" (the total snapshot count) vs "5 cycles" (the actual pattern count) reveals that the 2B was essentially pattern-matching on "data exists" rather than reasoning about the motion. The 8B at least identified the correct count and connected it to the right joint, even if it misread what the pattern represents.

**Anomaly severity is where the 8B shows better calibration.** Calling the 18.56 rad/s spike a CRITICAL_FAULT when your own explanation says "sensor artifact or numerical overflow" is inconsistent reasoning. A critical fault implies something went wrong with the robot that requires intervention. A sensor artifact means a data pipeline glitch that should be filtered. The 8B made the right distinction; the 2B filed a misleading severity level.

**The 8B is slower but more coherent.** 25.9s vs 10.6s — a 2.4x difference. The extra time produces better-structured reasoning, accurate event extraction, and a more honest assessment of what can and cannot be concluded from the data.

---

## Implications for Using Cosmos on Unlabeled Traces

Neither model is ready to autonomously identify robot task types from raw joint data alone. If Cosmos is being used as a motion auditor, the task context likely needs to be provided — but as a minimal hint ("this is a fastening operation") rather than a full annotation of every joint. The current experiment suggests that with zero context, both models will produce plausible-sounding but factually wrong task identifications.

For structured data extraction (gripper event timestamps, anomaly classification, pattern counting), the 8B produces reliable output while the 2B produces unreliable output that looks correct on the surface. Any pipeline that consumes 2B outputs needs external validation; the 8B's outputs require less post-processing.
