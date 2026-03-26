# KPI Reference — How Metrics Are Calculated and Where

This document describes every key performance indicator (KPI) used in the screwing pipeline, how each one is computed, and where in the codebase that computation lives. The goal is to give anyone reading the results enough context to understand what the numbers actually mean, not just what they are called.

---

## Overview: Two Layers of Measurement

There are two distinct stages of measurement in the pipeline. The first is purely numerical — Python code reading raw joint angles from the recorded trace and computing statistics. The second is model-based — Cosmos 8B reading those pre-computed numbers alongside the trajectory and scoring the execution against a rubric. These two layers are intentionally kept separate: the numerical metrics act as a ground-truth anchor so the model can't hallucinate values that disagree with what actually happened.

---

## Layer 1 — Pre-Computed Trajectory Metrics

Everything in this layer is computed in `tests/run_8b_pipeline.py` inside the function `compute_trajectory_metrics()` (line ~466). The function takes the sampled trajectory JSON (80 evenly-spaced snapshots from the full trace) and returns a dictionary of strings that get injected directly into the analysis and evaluation prompts.

The raw data for each snapshot is:
- `timestamp` — seconds from task start
- `joints` — six joint angles in radians: [shoulder_pan, shoulder_lift, elbow, wrist_1, wrist_2, wrist_3]
- `gripper_state` — gripper opening in radians (0 = fully open, higher = more closed)


### 1.1 Total Screwing Rotation

**What it measures:** How many degrees the wrist rotated in the screwing direction (CCW) across all strokes.

**How it is calculated:** The wrist_3 joint (index 5 in the joints array) is extracted across all 80 samples. The first difference array `dw3` is computed. Any step where `dw3 > 0.01` counts as CCW (screwing). The total CCW rotation is the sum of absolute values of all those steps, converted from radians to degrees.

**Expected value:** With 5 cycles and a wrist range of ~11.97 rad, the expected total is approximately 3428°. Anything within ~5% of that is considered correct.

**Code location:** Lines 479–491, `compute_trajectory_metrics()`

**Why it matters:** If the total rotation is significantly less than expected, the robot is not completing all its screwing strokes — the screw won't be driven far enough in.


### 1.2 Screwing Efficiency Ratio

**What it measures:** What fraction of all wrist_3 travel was productive screwing vs. repositioning.

**How it is calculated:** The CCW (screwing) total and the CW (repositioning) total are both summed from `dw3`. The efficiency is `CCW_total / (CCW_total + CW_total) × 100`. In a perfect symmetric cycle, where one full CCW stroke equals one full CW reset, this value would be exactly 50%.

**Score thresholds injected into the model:**
- Below 45% → score 2
- 45–55% → score 3
- 55–70% → score 4
- Above 70% → score 5

**Code location:** Lines 484–487

**Why it matters:** Efficiency below 40% means the robot is spending more wrist motion resetting than actually driving the screw. This can happen if the step size is too small, cycles are cut short, or the robot does extra CCW motion for other reasons.


### 1.3 Joint Drift During Screwing Strokes

**What it measures:** How much joints 1–5 wander while wrist_3 is rotating during a screwing stroke. Ideally they should be completely stationary — only the wrist should move.

**How it is calculated:** The pipeline identifies each individual screwing block (a contiguous sequence of samples where `dw3 > 0.01`). For each block, it takes the j1–j5 values at the start and computes the maximum absolute deviation across all five joints across all samples within that block. Each block gets its own number, reported in radians.

**Score thresholds:**
- Worst block drift > 0.1 rad → score 2
- 0.02–0.1 rad → score 3
- 0.005–0.02 rad → score 4
- Below 0.005 rad → score 5

**Code location:** Lines 509–527

**Why it matters:** When the arm joints move during screwing, the screwdriver tip is shifting its position relative to the screw head. This is the most common failure mode — the IK solver adjusts the shoulder and elbow slightly on each step rather than rotating pure wrist, which means the tool drifts away from the engagement point mid-stroke.


### 1.4 CW Duration Coefficient of Variation (CoV)

**What it measures:** How consistent the screwing strokes are in terms of time. If all five strokes take roughly the same amount of time, CoV is low. If one stroke takes twice as long as another, CoV is high.

**How it is calculated:** The duration of each screwing block in seconds is measured as `timestamps[end] - timestamps[start]`. The standard deviation is divided by the mean and expressed as a percentage: `CoV = (std / mean) × 100`.

**Score thresholds:**
- CoV > 40% → score 2
- 15–40% → score 3
- 5–15% → score 4
- Below 5% → score 5

**Code location:** Lines 529–534

**Why it matters:** High CoV means MoveIt is finding different-length motion plans on different cycles, usually because the IK seeds are landing in slightly different configurations each time. On a production line this means the total cycle time is unpredictable, which creates downstream scheduling problems.


### 1.5 Engagement Position Repeatability

**What it measures:** How accurately the robot returns to the same screw engagement position at the start of each new screwing cycle.

**How it is calculated:** For each screwing block, the j1–j5 joint values at the start of that block are recorded. After all blocks, the mean across cycles is computed per joint. The maximum absolute deviation from this mean, per joint, is reported. The overall worst deviation (single worst joint, worst cycle) is what gets injected into the scoring constraints.

**Score thresholds:**
- Worst deviation > 0.1 rad → score 2
- 0.02–0.1 rad → score 3
- 0.005–0.02 rad → score 4
- Below 0.005 rad → score 5

**Code location:** Lines 552–557

**Why it matters:** Each screwing cycle starts with the robot re-lowering to the screw engagement height. If the IK solution is slightly different each time, the screwdriver tip contacts the screw head at a different angle and XY offset. Over five cycles, accumulated misalignment can cause the tip to slip off the screw head entirely.


### 1.6 Peak Wrist_3 Velocity

**What it measures:** The fastest the wrist joint moved at any single sample interval during the entire task.

**How it is calculated:** The velocity for each joint at each timestep is estimated as `Δangle / Δtime`. The maximum absolute value across all timesteps is taken per joint. The wrist_3 value (joint 5) is extracted specifically.

**Score thresholds:**
- Above 2.0 rad/s → score 2
- 1.0–2.0 rad/s → score 3
- 0.5–1.0 rad/s → score 4
- Below 0.5 rad/s → score 5

**Code location:** Lines 474–477

**Why it matters:** For the UR5e, high wrist velocity during screwing strokes suggests the robot is accelerating and decelerating harshly through each 45° step rather than moving smoothly. This generates vibration at the screwdriver tip, which reduces engagement quality and can cause the tip to bounce out of the screw head.


### 1.7 Return-to-Home Error (L2 Norm)

**What it measures:** How close the robot's final joint configuration is to where it started (the home position).

**How it is calculated:** The absolute difference between the last sample's joint angles and the first sample's joint angles is computed per joint. The L2 norm (Euclidean distance in joint space) of those six differences is reported.

**Score thresholds:**
- L2 > 0.05 rad → score 2
- 0.02–0.05 rad → score 3
- 0.005–0.02 rad → score 4
- Below 0.005 rad → score 5

**Code location:** Lines 549–550

**Why it matters:** A large home-return error means the robot is finishing in a slightly different configuration each run. On a repeated-cycle production line, this compounds — if it starts the next cycle from a different position, the IK solutions for approach and grasp will be slightly different, and repeatability degrades over time.


### 1.8 Productive Time and Idle Percentage

**What it measures:** How much of the total task duration was spent doing active screwing, and how much was the robot sitting still with no joints moving.

**How it is calculated:**
- Productive time: sum of durations of all identified screwing blocks.
- Idle time: sum of timestep intervals where all six joint velocities were below 0.001 rad/s simultaneously.
- Both are expressed as percentages of total task duration.

**Code location:** Lines 536–541

**Why it matters:** A productive time below 30% means the robot is spending the majority of its time in transit, settling, or idle. While some overhead is unavoidable, extremely low productive fraction suggests the velocity scaling or inter-step sleep times are too conservative.


### 1.9 Gripper Variation During Screwing

**What it measures:** How much the gripper opening value fluctuated from the moment the screwdriver was gripped until the end of the task.

**How it is calculated:** The index where the gripper first closes (value > 0.5) is found. The range (max - min) of gripper values from that index onward is reported.

**Code location:** Lines 543–547

**Why it matters:** Any variation in gripper state after the screwdriver is grasped suggests the grip is not stable. Even a small fluctuation (0.02+ rad) means the fingers are shifting slightly, which rotates the screwdriver in the gripper and changes the effective tip position relative to the screw head.


---

## Layer 2 — Model-Based Scoring (Cosmos 8B Evaluation)

The evaluation is performed by Cosmos-Reason2-8B via the eval prompt templates in `prompts/eval_fault_detection.txt`, `prompts/eval_energy_dynamics.txt`, and `prompts/eval_process_quality.txt`. Each prompt feeds the model the Cosmos 2B reasoning output, the raw sampled trajectory, and all the pre-computed metrics from Layer 1. The model then scores 13 sub-categories.

The eval prompts are filled and dispatched in `run_8b_pipeline.py` inside the `main()` function, Stage 2 (approximately line 624 onward). The model response is parsed back as JSON and the `overall_score` is extracted.


### Scoring Rubric — 13 Sub-Categories

Each sub-category is scored 1–5. The `overall_score` is a weighted average, not a simple mean. The weights are defined explicitly in the prompt.

#### Group A: Reasoning Quality (assessed against the 2B analysis output)

**Spatial Awareness (weight: 1%)**
Did the 2B model correctly identify the screwdriver location, the screw target location, the approach heights, and the workspace geometry? This is mostly a sanity check — if the analysis completely misidentifies where things are, nothing else can be trusted.

**Phase Identification (weight: 2%)**
Did the 2B model correctly segment the trajectory into pick / screw cycles / return phases, with approximately correct timestamps? This checks whether the model read the trajectory with the right mental model of the task structure.

**Screwing Mechanics Understanding (weight: 5%)**
Did the 2B model demonstrate that it understands *why* the task is structured the way it is — specifically that CCW reposition can only happen while lifted (otherwise it unscrews the screw), and that the cyclic pattern exists because wrist_3 has a finite joint range. Higher-level mechanical comprehension, not just phase labelling.

**Safety Awareness (weight: 8%)**
Did the 2B model flag anything related to joint limit proximity, gripper stability, collision risks during the pick-to-screw transit, and the constraint that CCW must never happen at engage height? This is what justifies giving the model safety responsibility — it needs to demonstrate that it's actually thinking about these constraints.

#### Group B: Pick Phase Quality

**Grasp Execution (weight: 10%)**
Was the pick motion correct? This covers the approach being top-down, the descent reaching the right height (~0.26 m tool0 z), the gripper closing to approximately 0.57 rad, and the retreat being clean and vertical. The model scores this directly from the trajectory.

**Pick-to-Screw Transition (weight: 2%)**
Was the transit from the screwdriver pick position to above the screw target smooth, without unnecessary detours, and with the top-down orientation maintained throughout? A common failure here is the orientation flipping during transit if the IK seed is poor.

#### Group C: Screwing Phase Quality

**Screw Engagement (weight: 10%)**
Did the robot lower to the correct height (0.26 m) at the correct XY position (0.40, 0.10) before starting each screwing stroke? Misalignment here means the screwdriver tip is offset from the screw head and will slip during rotation.

**CW Stroke Quality (weight: 15%)**
The highest-weighted single sub-category. This checks whether joints 1–5 stayed approximately still during each screwing stroke while only wrist_3 rotated, whether the direction was correct, whether the step size was consistent, and whether the wrist stayed within safe limits. The pre-computed drift values from Layer 1 are injected into this sub-category as hard constraints — the model is told what score it must give based on the measured drift numbers.

**Lift-Reposition-Reengage Quality (weight: 12%)**
Between every pair of screwing strokes, the sequence must be: lift → CW reset → lower back to engage. This checks that all five transitions followed that sequence correctly, that the lift cleared the screw head before the CW reset happened, and that re-engagement landed at the same position as before.

**Cycle Completion (weight: 20%)**
The most heavily weighted criterion. Did all five cycles complete? Was the total screwing rotation close to the expected value (~3428°)? The pre-computed total rotation from Layer 1 is injected here. If the rotation is far off or cycles were cut short, this drags the overall score significantly.

#### Group D: General Motion Quality

**Smoothness (weight: 4%)**
Were joint transitions gradual throughout the task, or were there sudden large jumps in joint angle between consecutive timesteps? Large jumps indicate the motion planner produced discontinuous trajectories or the velocity scaling was set too aggressively.

**Efficiency (weight: 3%)**
Did the robot use appropriate velocity for each phase — slow and controlled for engagement and screwing, faster for transit and repositioning? Uniform slow speed throughout is flagged as inefficient; appropriate variation gets a high score.

#### Group E: Reasoning–Trajectory Alignment

**Consistency (weight: 8%)**
Does the 2B reasoning output actually describe what the trajectory shows? The 8B model reads both and checks whether the phase descriptions, rotation directions, joint configurations, and timing claims in the reasoning match the numbers in the trajectory. This is what catches cases where the 2B model described an ideal execution rather than what actually happened.


### Overall Score Computation

The `overall_score` is the dot product of the 13 sub-category scores and their weights:

```
overall_score = 0.01 × spatial_awareness
              + 0.02 × phase_identification
              + 0.05 × screwing_mechanics
              + 0.08 × safety_awareness
              + 0.10 × grasp_execution
              + 0.02 × pick_to_screw_transition
              + 0.10 × screw_engagement
              + 0.15 × cw_stroke_quality
              + 0.12 × lift_reposition_reengage
              + 0.20 × cycle_completion
              + 0.04 × smoothness
              + 0.03 × efficiency
              + 0.08 × consistency
```

The result is a float between 1.0 and 5.0. All scores seen in practice cluster above 4.5 because the screwing task is well-defined and the 2B analysis is generally structurally correct — the differentiation happens in CW stroke quality and cycle completion.


### Critical Failure Conditions

Separately from the scoring rubric, the eval prompt defines six conditions that constitute an automatic critical failure regardless of what scores were assigned. These are:

1. Gripper state changed during any screwing cycle — screwdriver likely dropped
2. CCW wrist rotation happened at engage height — actively unscrewing the screw
3. Wrist_3 exceeded safe limits at any timestep
4. Fewer than one complete screwing stroke was executed
5. Tool orientation deviated more than 15° from top-down during screw engagement
6. Robot did not visit both the screwdriver location and the screw target location

Critical failures are reported in the `critical_failures` array in the JSON output. They do not automatically override the overall score, but they are used in the best-evaluation selection logic and reported prominently in the pipeline output.


### How the Scores Are Used

After evaluation, the pipeline selects the prompt that produced the highest `overall_score` across the three eval runs (fault_detection, energy_dynamics, process_quality). That selected evaluation's `suggestions` array and `overall_score` are then passed into the waypoint generation prompt in Stage 4. The 8B model receives the feedback from its own evaluation and uses it to propose improved motion parameters for the next execution. This creates the closed feedback loop that is the core thesis demonstration.


---

## Where to Find Everything

| Component | File | Approximate Lines |
|-----------|------|-------------------|
| Metric computation (all 9 metrics) | `tests/run_8b_pipeline.py` | 466–639 |
| Screwing block detection | `tests/run_8b_pipeline.py` | 493–517 |
| Scoring constraint generation | `tests/run_8b_pipeline.py` | 559–616 |
| Fault detection eval prompt + rubric | `prompts/eval_fault_detection.txt` | entire file |
| Energy dynamics eval prompt + rubric | `prompts/eval_energy_dynamics.txt` | entire file |
| Process quality eval prompt + rubric | `prompts/eval_process_quality.txt` | entire file |
| Eval dispatch + score parsing | `tests/run_8b_pipeline.py` | ~830–890 |
| Best-eval selection | `tests/run_8b_pipeline.py` | ~892–915 |
| Waypoint generation using eval feedback | `tests/run_8b_pipeline.py` | ~947–1035 |
