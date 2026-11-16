# Experiment: Qwen thinking mode on the UNO Q

Run on 16 November 2026 on the board (Arduino UNO Q, 4x Cortex-A53, CPU only),
with the kiosk and app running. Production was not changed: the experiment used
a second llama-server (port 9998) started from the same binary and model file
inside the `arduino:llm` runner container, then stopped and removed.

## 1. How thinking is supported in our deployment

| Question | Finding (observed on the board) |
|---|---|
| Runtime | llama.cpp b11950 (`f4d8af608`), `llamacpp-runner` 0.13.2, 1 slot, n_ctx 4096 |
| Production mode today | **Thinking off.** The app sends no `enable_thinking`; the model's chat template then inserts an empty `<think>\n\n</think>` block |
| Switch | Per request: `chat_template_kwargs: {"enable_thinking": true}`. The template then opens `<think>\n` (prompt is 2 tokens shorter) |
| Reasoning output | `--reasoning-format` (default `auto`) moves reasoning into `message.reasoning_content`; `reasoning_format: "none"` per request shows the raw tags |
| Budget | `--reasoning-budget N` exists, but only as a server start flag; it would mean changing the Brick's launch |
| JSON schema with thinking | Worked: no schema failures in 68 thinking requests, because no reasoning was produced to conflict with it |
| **Does Qwen3.5-0.8B actually think?** | **No.** In every thinking request (68 benchmark requests and 8 probes) it closed the think block immediately: `reasoning_content` was always empty and `<think>\n</think>` came back with no content. This held for greedy decoding, Qwen's precise-task sampling (T 0.6, top_p 0.95, top_k 20), its general-task sampling (T 1.0, presence penalty 1.5), with our system prompt, with a prompt asking it to think, and with a bare maths question and no system prompt |
| Reasoning loops | Not inside a think block (there was none). On the bare question "2:45 PM + 50 minutes" it reasoned in the visible answer, looped ("Wait, 2:45 + 50 minutes = ...") and hit 400 tokens after 100 s with a wrong result |

Third-party copies of the model card say Qwen3.5-0.8B runs without thinking by
default and that `enable_thinking` turns it on; the board shows that this
switch does not produce a reasoning trace with this model and quantisation.

## 2. Method

- **Dataset** (`scenarios.py`): 17 synthetic scenarios with known truth,
  covering all 9 required types (sensor notices, upcoming events, event plus
  weather, morning and evening briefings, several events, multi-source
  questions, missing or stale data, contradictory or ambiguous data, and
  insufficient information).
- **Configurations** (`bench.py`), same model, server and scenarios:
  - **A**: the production request byte for byte (app prompt, greedy,
    `max_tokens` 100, JSON schema).
  - **B**: A plus `enable_thinking: true`, `max_tokens` 1024.
  - **B′**: B with Qwen's thinking sampling, seeds 1 and 2.
  - **C**: thinking off; the same snapshot plus number-free hints (for example
    `rain_likely_today`, `next_event_during_later_forecast`,
    `room_temperature_vs_preference`) and an `unavailable` list, with one
    extra prompt rule.
- **Runs**: 2 repeats × 17 scenarios × 4 configurations = 136 requests. Each
  configuration ran as a block, so the cached prompt prefix behaves as in
  production.
- **Scoring**:
  - The app's own `validate_response`.
  - Automatic fact checks.
  - A hand grade of every distinct reply (`manual_grades.py`): **G** correct
    and helpful, **P** correct but incomplete, **W** wrong, invented or
    garbled.

## 3. Results (measured)

| Config | Good | Partial | Wrong | Shown by app (passes validator) | **Wrong but shown** | Latency mean / median / p90 / max (s) | Output tokens (mean) | Reasoning tokens |
|---|---|---|---|---|---|---|---|---|
| A production | **11** | 7 | 16 | 27 | 9 | 17.3 / 16.7 / 25.9 / 44.8 | 31.6 | 0 |
| B thinking, greedy | 8 | 8 | 18 | 27 | 11 | 16.3 / 17.2 / 22.2 / 27.1 | 30.7 | 0 |
| B′ thinking, Qwen sampling | 4 | 10 | 20 | 20 | 6 | 17.6 / 18.1 / 23.5 / 28.5 | 35.5 | 0 |
| C richer context | 8 | 4 | 22 | 23 | 11 | 18.9 / 17.2 / 26.8 / 43.0 | 32.7 | 0 |

Each configuration had 34 runs.

- **Timing** (identical across configurations, within noise):
  - Prompt reading 10.3–10.4 tokens/s; generation 4.0 tokens/s.
  - Warm prompt time 9.2 s median for A, B and B′, and 11.4 s for C (55 more input tokens).
  - Generation 7.4–8.0 s median.
- **Cold vs warm:** only the first request had an empty cache: 44.8 s, with all 357 prompt tokens read. Warm requests reuse the system prompt.
- **No truncations or timeouts:** none in any configuration. Thinking never used its larger token budget.
- **Resources** while the experiment server ran:
  - 3.2 CPU cores on average (3.6 max).
  - RSS 690–810 MB, including the shared mapped model file.
  - At least 1.45 GB of memory still available; load average up to 6.2.
- **Run-to-run noise:** greedy configurations gave different text on the
  second repeat in 27 of 51 cases. The prompt cache changes the arithmetic
  slightly, which changes greedy choices. So differences of 2–3 answers
  between configurations are within noise.
- **Missing-data scenarios** (`missing_calendar`, `stale_weather`): every
  configuration invented an answer. In production these never reach the
  model (no valid evidence means the factual fallback is shown), so they test
  the model, not the app.

### Examples

- **Thinking made it worse:**
  - `comfort_humid`, B′: "The room feels cool and comfortable with a 50% humidity chance today." Humidity was 68%, above the range; the validator rejected it.
  - `prep_imminent`, B′: "...8 minutes, which is well under the 25-word preparation limit."
- **Thinking helped once:** `insufficient_location`, B′: "Your lunch meeting is scheduled for 12:30 PM. Check your calendar to see where to go." A, B and C all said "There is no lunch meeting scheduled for today".
- **Better context helped:** `q_umbrella_next_event`, C: "...the facts indicate you will be in a rainy forecast later today". This was the only answer giving the right reason. A and B said "cloudy with 20% rain chance later today", but the later forecast was 70%.
- **Better context did not help:**
  - `q_room_before_meeting`, C: "comfortable ... as the temperature is above the preferred range". The hint said "above", and every configuration still called 27.4 °C (range 20–26) "comfortable".
  - `contradictory_forecast`: every configuration said rain later. The 60% chance was now; later was 10%.
  - `briefing_evening` (no events left): every configuration invented a next event.

## 4. Conclusion

**Does thinking make ContextCue more intelligent? No, not with this model.**
Qwen3.5-0.8B produces no reasoning trace on this runtime, so "thinking on"
only shortens the prompt by 2 tokens and, with Qwen's sampling, adds
randomness. Measured quality dropped: 11 good answers for A, 8 for B and 4 for
B′. Latency was unchanged, since no reasoning tokens were produced. The
hypothesis that it would help with complex multi-source questions is not
supported: multi-source scenarios were 4/4 acceptable for A and C, 2/4 for B
and 3/4 for B′.

**Recommendation: leave thinking disabled** (the current production setting).
Do not restrict it to complex questions either, because it showed no benefit
there.

Better context (C) was not a clear win: it helped on the umbrella question
and the sensor notices, but its hints introduced new mistakes. Do not adopt it
as it stands.

## 5. What did matter (for follow-up, not changed)

The failures are reasoning limits of a 0.8B model, and some of them get past
the validator: 9 wrong answers were shown in configuration A. The ones that
reach the screen, and the fixes that would catch them:

1. **Invented "Rain chance is 0%"** in preparation notices. The validator
   accepts "0" because "10:00" contains "00". Fix: treat time digits as times
   only.
2. **"Comfortable" when out of range.** Fix: reject "comfortable" in comfort
   and room answers when the code says the reading is above or below the range.
3. **Misread "later" forecasts (10% vs 70%)** and **wrong times ("11:00" for
   11:05)**. Fix: in comparison questions, code already knows the answer, so
   use it.
4. **Runaway repetition under 400 characters**, and **non-English fragments
   (`,orea`)** that pass the character check. Fix: a repeated-phrase check and
   a dictionary-word check.

These are code-side checks, which the plan already favours: code computes the
answer and the model only words it. They would cut the "wrong but shown"
column without any model change.

## Files

- `probe.py`: thinking mechanism probes.
- `scenarios.py`: the dataset.
- `bench.py`: the harness. It refuses the production port 9999.
- `analyze.py`: the automatic tables.
- `manual_grades.py`: the hand grades.
- `results.jsonl`: all 136 raw results.
- `res.log`: CPU and memory samples.

To reproduce: start a second llama-server on port 9998 in the runner
container, copy these files into the app folder, run `bench.py` inside
`contextcue-main-1`, and then stop the server and delete the copies.
