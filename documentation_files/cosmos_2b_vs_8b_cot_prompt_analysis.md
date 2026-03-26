# Cosmos-Reason2 Model Comparison: Chain-of-Thought Prompt
**Trace ID:** `569c9cf5`
**Models tested:** `nvidia/Cosmos-Reason2-2B` vs `nvidia/Cosmos-Reason2-8B`
**Prompt version:** Chain-of-thought + event definition fix + consistency instruction + temperature 0.2
**Goal:** Improve 2B accuracy by forcing step-by-step reasoning before conclusions

---

## What Changed in the Prompt

Three additions on top of the previous nudged prompt:

1. **Chain-of-thought instruction** — added at the top: "Before answering each question, explicitly list the data points from the snapshots that support your answer. Show your reasoning first, then state your conclusion."

2. **Gripper event definition** — added: "A gripper event is ONLY a timestamp where the gripper value changed from its previous snapshot. Do not list timestamps where the gripper value stayed the same."

3. **JSON consistency check** — added: "Your JSON summary must be fully consistent with your prose answers. Before writing the JSON, re-read your answers above and verify they match."

4. **Temperature lowered** from 0.6 → 0.2 for more deterministic output.

Q1 was also restructured into explicit sub-steps (a–d) to guide the model toward examining the right joints before drawing a conclusion.

---

## What Improved

### Anomaly severity — fixed for both models

This was the clearest win. In the previous run, 2B filed `CRITICAL_FAULT` in JSON while saying "sensor artifact" in prose — a direct contradiction. With the consistency instruction, both models now answer `SENSOR_ARTIFACT` in both prose and JSON.

The 8B also did the actual math this time: "average velocity implied by the position change from t=108.00s to t=114.09s is approximately 1.58 rad/s, which does not match the +18.5645 rad/s reading." That is the correct calculation and the correct conclusion. The 2B attempted the same calculation but got it backwards — it stated the calculation "gives +18.5645 rad/s," which is wrong arithmetic (position delta of 8.78 rad over 6.09s ≈ 1.44 rad/s, not 18.56), but still landed on SENSOR_ARTIFACT. The consistency instruction stopped it from contradicting itself even when the underlying math was wrong.

### 2B prose-to-JSON consistency — improved for Q5

The 2B now consistently classifies the anomaly across its prose and JSON. This is a direct result of the consistency check instruction. Previously, the 2B's JSON routinely contradicted its prose; this was the most reliable failure mode to fix through prompting.

---

## What Did Not Improve

### 2B gripper events — still broken

Despite the explicit definition ("a gripper event is ONLY a timestamp where the gripper value changed"), the 2B listed five gripper events at t=73.59s, 89.60s, 105.61s, 114.09s, and 120.00s — all snapshots where the gripper was unchanged at 0.5700. The actual close at t=7.24s and open at t=202.20s are both absent.

The explicit definition was fully ignored. This suggests the 2B's failure here is not a misunderstanding of the word "event" — it's a deeper issue with how the model reads and tracks the gripper value across snapshots. It is not comparing consecutive values; it is pattern-matching on snapshots that contain the gripper field and generating plausible-sounding entries.

The 8B continues to produce the correct two-entry list: close at t=7.24s, open at t=202.20s.

### Q1 — sub-steps backfired for both models

The step-by-step sub-questions introduced a regression. Sub-step (b) asked: "which joint accumulates the most total rotation across the trace?" The intended answer was `wrist_3`, which spins through multiple full revolutions. But both models answered `shoulder_lift` — because they interpreted "total rotation" as net displacement rather than total distance traveled, and `shoulder_lift` shows a monotonic shift from -1.57 to -1.73 rad while `wrist_3` oscillates back and forth.

The sub-questions meant to guide the models toward `wrist_3` accidentally guided both of them away from it. The 8B, which had correctly identified `wrist_3` as the fastening axis in the previous prompt run (without sub-steps), now answers `shoulder_lift` instead. The scaffolding made this question harder, not easier.

Neither model identified `wrist_3` as the screwing joint in this run.

---

## Side-by-Side: Before vs After

| | 2B — nudged | 2B — CoT | 8B — nudged | 8B — CoT |
|---|---|---|---|---|
| Response time | 7.0s | 11.0s | 25.0s | 29.0s |
| Fastening joint identified | Elbow (wrong) | shoulder_lift (wrong) | wrist_3 ✓ | shoulder_lift (regression) |
| Anomaly severity — prose | CRITICAL_FAULT | SENSOR_ARTIFACT ✓ | SENSOR_ARTIFACT ✓ | SENSOR_ARTIFACT ✓ |
| Anomaly severity — JSON | CRITICAL_FAULT | SENSOR_ARTIFACT ✓ | SENSOR_ARTIFACT ✓ | SENSOR_ARTIFACT ✓ |
| Prose/JSON consistent | No | Yes ✓ | Yes ✓ | Yes ✓ |
| Gripper event count | 3 (fabricated) | 5 (fabricated) | 2 ✓ | 2 ✓ |
| Gripper events correct | No | No | Yes ✓ | Yes ✓ |
| Recurring pattern count | 5 ✓ | 5 ✓ | 5 ✓ | 5 ✓ |

---

## Takeaways — CoT v1

**The consistency instruction works.** Asking the model to re-read its prose before writing JSON eliminated the prose/JSON contradiction in the anomaly field for the 2B. This is a reliable, low-cost fix.

**The event definition does not work for 2B.** The 2B ignores the explicit instruction about what "event" means and continues listing unchanged snapshots. This is not a comprehension problem that can be solved through prompting. The 2B apparently cannot track state changes across sequential snapshots — it reads each snapshot independently rather than comparing it to the previous one.

**Sub-step scaffolding needs care.** Breaking Q1 into sub-steps caused a regression in the 8B (which had previously gotten the answer right) and did not help the 2B. The specific framing of step (b) — "which joint accumulates the most total rotation" — was ambiguous and led both models to the wrong joint. Structured sub-questions only help if each sub-step is unambiguous and leads toward the right answer.

---

## Round 2 — CoT v2 (velocity-based Q1 + gripper fill-in table)

Two further fixes applied:

1. **Q1 step (b) reframed** — changed from "which joint accumulates the most total rotation" (ambiguous, led both models to `shoulder_lift`) to "which joint shows the highest sustained non-zero velocities for the longest stretches of time — look at the velocity column." Velocity is unambiguous: `wrist_3` consistently shows -0.6 rad/s during screwing phases; every other joint is near zero.

2. **Gripper fill-in table** — added an explicit row-by-row table for the model to fill in, comparing each snapshot's gripper value to the previous one. This forces sequential tracking rather than letting the model generate plausible-sounding events from scratch.

### What the fill-in table achieved for 2B

The 2B now lists **2 gripper events** at **t=7.24s and t=202.20s** — the correct timestamps. After four failed runs producing anywhere from 3 to 23 fabricated events, the forced table finally got the count and timestamps right.

The event type labels are still inverted — it files `OPEN` at t=7.24s (value 0.57) and `CLOSE` at t=202.20s (value 0.0), which contradicts itself since 0.57 is the closed position. But this is a much smaller error than what came before. The timestamps are now correct.

### What the velocity question achieved for both models

- **8B** recovered: it correctly identified `wrist_3` as having the highest sustained velocities (listing -0.6287, -0.6040, -0.5901 rad/s across screwing phases). This reverses the regression caused by the previous sub-steps.
- **2B** still named `elbow_joint` — even when explicitly asked to look at velocities, it cited elbow velocities of ~0.001 rad/s as "sustained non-zero." The elbow barely moves; the 2B appears unable to compare magnitudes across joints and identify the dominant one.

### Anomaly math — both models now show the calculation

The explicit sub-question for Q5 ("calculate the average velocity from position change") worked for both:
- **8B**: `(5.9798 - (-2.8025)) / (114.09 - 108.00) = 8.7823 / 6 = 1.4637 rad/s` — correct arithmetic, correct conclusion.
- **2B**: `≈+1.58 rad/s, does not match +18.56 rad/s` — correct conclusion, slightly imprecise arithmetic but correct reasoning.

Both now consistently file `SENSOR_ARTIFACT` in prose and JSON.

---

## Full Progression Summary

| | Nudged | CoT v1 | CoT v2 (fixed) |
|---|---|---|---|
| **2B** — fastening joint | Elbow ❌ | shoulder_lift ❌ | Elbow ❌ |
| **2B** — gripper event count | 3 fabricated ❌ | 5 fabricated ❌ | 2 ✓ |
| **2B** — gripper timestamps | Wrong ❌ | Wrong ❌ | t=7.24s, t=202.20s ✓ |
| **2B** — gripper event types | Wrong ❌ | Wrong ❌ | Swapped ⚠ |
| **2B** — anomaly severity consistent | No ❌ | Yes ✓ | Yes ✓ |
| **2B** — anomaly math | No ❌ | Wrong ❌ | Correct ✓ |
| **8B** — fastening joint | wrist_3 ✓ | shoulder_lift ❌ (regression) | wrist_3 ✓ (recovered) |
| **8B** — gripper events | Correct ✓ | Correct ✓ | Correct ✓ |
| **8B** — anomaly severity | Correct ✓ | Correct ✓ | Correct ✓ |
| **8B** — anomaly math shown | No | Yes ✓ | Yes ✓ |

**2B remaining failures:** cannot identify `wrist_3` as the dominant spinning joint even when explicitly asked to look at velocities; gripper event type labels still inverted.

**8B remaining gaps:** describes `wrist_3` cycling as "fine adjustments" rather than screwing; doesn't yet explain the resets as joint-limit recovery.

**Conclusion:** The fill-in table was the most effective single change — it fixed 2B gripper extraction from completely wrong to timestamps-correct. The velocity-focus Q1 recovered the 8B regression. The 2B's inability to identify `wrist_3` from velocity data alone appears to be a training gap, not a prompting problem.
