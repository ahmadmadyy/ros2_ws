# Cosmos-Reason2: 2B vs 8B — Model Comparison
**Hardware:** UR6 arm + Robotiq 85 gripper
**Task:** Continuous top-down screwing operation
**Trace:** `569c9cf5` — 211 seconds, 20,225 joint snapshots at 95.8 Hz
**Prompt:** Open-ended with domain hint ("fastening operation"), velocity fill-in table, chain-of-thought reasoning, gripper event fill-in table, consistency instruction, temperature 0.2

---

## The Test

Both models received the same raw joint data — no joint annotations, no task label beyond "this is a fastening operation." They were asked to identify the fastening technique from the data, break down what each key joint was doing, describe the recurring motion pattern, analyze a velocity anomaly, and rate the execution quality. Answers had to be grounded in specific timestamps and values before drawing conclusions.

The trace has a clear structure: `wrist_3` spins clockwise to drive a screw, accumulates ~5.8 radians of rotation, then reverses counterclockwise for ~8 seconds to unwind before the next screwing block. This happens five times. The gripper closes at t=7.24s to grab the screwdriver and opens at t=202.20s to release it. At t=110.05s, `wrist_3` reports +18.5645 rad/s — position data from adjacent samples shows actual motion at ~1.44 rad/s, making this a sensor artifact.

The prompt included two fill-in tables: one requiring the model to look up and compare peak velocity values for each joint before drawing a conclusion about which joint drives the fastening action, and one requiring it to track gripper state changes row by row.

---

## Task Identification

**Both models correctly identified `wrist_3` as the primary fastening joint** — the first time the 2B got this right across all prompt versions.

**8B** filled the velocity table accurately and drew the correct conclusion cleanly: "`wrist_3_joint` at 18.5645 rad/s" as the dominant joint, then noted its velocity is "predominantly negative during the main working phases, indicating continuous rotation in one direction" and concluded this is a "rotary fastening technique, likely involving a screwdriver or similar tool." That is the most accurate task description either model has produced across the entire test series.

**2B** also identified `wrist_3` as having the highest peak velocity "by a clear margin" and correctly connected sustained negative rotation to a rotational fastening technique. The velocity table prompted it to scan and compare magnitudes it had previously ignored — the same mechanism that fixed gripper event extraction in an earlier run worked here too.

The 8B used the anomaly spike value (18.5645 rad/s) as the peak for `wrist_3` in its table, which is technically the highest single reading but is a sensor artifact rather than operational velocity. The normal screwing velocity is around -0.6 rad/s. The conclusion was still correct, but a sharper analyst would note that the spike shouldn't be treated as representative of the joint's operational range.

---

## Joint Role Breakdown

**wrist_3:**
- 8B: "Likely responsible for the fastening action, such as tightening a screw." Correctly connected the joint's cycling between -5.80 and +5.98 rad to the fastening task.
- 2B: "Exhibits oscillatory motion, dominating the trace with continuous rotation." Gets the dominant motion right but doesn't explicitly connect it to screwing.

**shoulder_lift:**
- Both models correctly identified it as the vertical approach joint, noting the position shift from -1.57 to -1.55 rad as the arm descends to the workpiece between t=0s and t=7.24s.

**Gripper:**

This is where both models had issues in this run.

The 2B's Q3 prose said the gripper "remains static at 0.0000 rad throughout the trace, indicating no opening or closing action" — it completely missed both state changes and apparently did not fill in the row-by-row table that was in the prompt. The JSON reverted to 23 entries, one per snapshot. The progress from the previous run (where the fill-in table produced correct timestamps) was lost.

The 8B got the timestamps right — it noted the gripper changes at t=7.24s and t=202.20s — but had the event labels inverted: it filed `OPEN` at t=7.24s (value 0.5700) and `CLOSE` at t=202.20s (value 0.0000). A value of 0.5700 on the Robotiq 85 knuckle joint means fully closed, not open. The 8B correctly identified the moments of state change but misread the direction. In previous runs it had gotten this right; having two fill-in tables in the prompt at the same time appears to have diluted attention to the gripper table.

---

## Recurring Pattern

Both models identified five cycles involving `wrist_3`. The 8B cited specific values (-5.7958 to +5.9822 rad, ~12 second cycle duration). The 2B described the same joint and value range, also estimating ~6 seconds per cycle. The actual reset cycles last ~8.4 seconds each with ~40 seconds of screwing between them — both estimates are off, but both correctly identified the right joint and pattern count.

---

## Anomaly at t=110.05s

Both models handled this correctly and consistently.

The 8B showed the calculation: (5.9798 − (−2.8025)) ÷ 6 = 1.464 rad/s, concluded this doesn't match 18.5645, classified as `SENSOR_ARTIFACT`. Prose and JSON matched.

The 2B stated "the average velocity from t=108.00s to t=114.09s yields approximately +18.56 rad/s, matching the reported value" — incorrect arithmetic — but then still concluded `SENSOR_ARTIFACT` based on the velocity spike not correlating with the position change. The JSON was consistent with the prose.

---

## Execution Quality

| | Cosmos-Reason2-2B | Cosmos-Reason2-8B |
|---|---|---|
| Response time | 13.1s | 31.5s |
| Fastening joint identified | wrist_3 ✓ | wrist_3 ✓ |
| Task described as screwing | Implicitly ("rotational fastening") | Explicitly ("screwdriver-like tool") |
| wrist_3 role understood | Partially | Yes |
| Reset pattern — count | 5 ✓ | 5 ✓ |
| Gripper close timestamp | Absent ✗ | t=7.24s ✓ |
| Gripper open timestamp | Absent ✗ | t=202.20s ✓ |
| Gripper event types | All labeled CLOSE, 23 entries ✗ | 2 entries, labels inverted ⚠ |
| Anomaly classification | SENSOR_ARTIFACT ✓ | SENSOR_ARTIFACT ✓ |
| Anomaly math | Incorrect arithmetic, right conclusion | Correct ✓ |
| Verdict | CONDITIONAL PASS 6.0/10 | CONDITIONAL PASS 6.5/10 |

---

## What Separates Them

**Task identification — both models now get it.** The velocity fill-in table resolved the 2B's persistent failure to identify `wrist_3`. By forcing it to look up a specific number for each joint and write them in a table, the comparison became unavoidable: `wrist_3` at 18.5645 rad/s against everything else below 0.6 rad/s. The same mechanism that fixed gripper event extraction in a previous run — replacing open-ended reasoning with a mechanical template — worked here too.

**Depth of understanding still differs.** The 8B explicitly named this a "rotary fastening technique involving a screwdriver," connected the wrist's negative velocity to clockwise tightening, and explained the vertical approach correctly. The 2B correctly pointed at `wrist_3` but described it generically as "oscillatory motion with continuous rotation" — it identified the right joint without fully explaining what it's doing or why.

**Gripper extraction — a tradeoff appeared.** The previous run showed that a row-by-row gripper fill-in table gets the 2B to correct timestamps. This run had both the velocity table and the gripper table, and the 2B ignored the gripper table entirely — reverting to 23 fabricated events. The 8B got the timestamps right but inverted the labels. Having two structured templates in the same prompt appears to split attention; the models complete one and shortcut the other. The velocity table was prioritized over the gripper table in both cases.

**Anomaly handling remains solid for both.** `SENSOR_ARTIFACT` consistently in prose and JSON for both models, regardless of whether the arithmetic is exact.

---

## Bottom Line

The velocity fill-in table closed the most persistent gap between the two models: the 2B can now identify the primary fastening joint from data, which it could not do in any previous prompt version. The core capability difference that remains is depth — the 8B understands what the joint is doing mechanically, the 2B identifies which joint without fully explaining the mechanism. For structured outputs, the 8B remains more reliable on gripper semantics. For task identification, the two models are now much closer than they were at the start of this test series.
