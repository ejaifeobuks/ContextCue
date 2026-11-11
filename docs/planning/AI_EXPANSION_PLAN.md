# ContextCue AI Expansion Plan

Status: proposal, 8 November 2026. Nothing here is implemented yet, apart from
the latency fix in section 1.

## 1. Where the AI stands

- **Model:** Qwen3.5-0.8B Q4_0 through the `arduino:llm` Brick (llama.cpp
  0.13.2, **CPU only**, 4 threads, 2.5 GB limit).
- **Measured on the board (8 November):**
  - The model reads input at 7-11 tokens/s and writes 2-4 tokens/s.
  - After the text-only schema fix, a request takes 12-19 s (it was 32-45 s,
    with timeouts).
- **Current job:** the model rewrites one notice (morning, preparation,
  comfort, or one of four fixed questions) from a compact snapshot.
  - Code decides when to speak, what is true and what advice applies.
  - The model only words the notice, and the validator checks every number.

The weak point is not the model's wording. The AI only sees one notice's
facts, never acts unprompted beyond four triggers, and can only be asked four
canned questions. A 0.8B model cannot become a general assistant on this CPU.
The way forward is to keep **code as the reasoner** and the **model as
classifier and writer**, and to give both richer context and more ways in.

## 2. Design rules (unchanged)

1. Code computes facts (times, counts, trends, thresholds). The model never
   does arithmetic or invents facts. The validator stays.
2. Small, constrained outputs: a JSON schema with enums wherever possible.
   Grammar-constrained *classification* is where small models are reliable.
3. Every request runs in the background, and the UI never waits on the model:
   factual text first, AI wording when it arrives.
4. Cloud services are opt-in only, with a visible label ("Cloud"), and are
   never required for the demo.

## 3. Proposed features, in priority order

### P1. Streaming and "factual first, AI upgrade" (latency you do not see)

- **What:** show the factual text at once, marked "Writing…". Stream the
  model's tokens into the card, then swap to the validated text with the "AI"
  label. Validation runs on the finished text. A rejected reply goes back to
  the factual text.
- **Why:** 12-19 s feels slow; text appearing at once and filling in reads as
  "thinking". It also removes the deadline failure mode from the user's view.
- **How:** use `"stream": true` on the existing endpoint (the LLM Brick
  supports streaming), with server-sent events or the 1 s poll to the UI.
  About 1 day of work.

### P2. Ask anything (intent router plus fact handlers)

- **What:** a free-text or spoken question is mapped by the model onto an
  **enum of intents**, through a schema with an `intent` field plus slots:
  - next_event, rain_today, rain_at_time, room_comfort, schedule_today,
    free_time
  - leave_by, tomorrow_first_event, weather_tomorrow, set_reminder,
    unsupported
- **Then:** code answers from facts, and the model words the answer.
  `unsupported` replies honestly ("I can answer about your calendar, weather
  and room").
- **Why:** classification into about 12 labels is a short, grammar-forced
  output (about 5 tokens, about 3 s). It turns four buttons into a
  conversational surface without letting the model invent answers.
- **Input:** the existing on-screen keyboard, the suggestion chips, and voice
  (P4).
- **Effort:** 2-3 days, including an evaluation set of about 60 phrasings per
  intent (extends `scripts/ai-eval.py`).

### P3. Richer, proactive context (code-computed, model-worded)

All of these are deterministic calculations on data the app already has. Each
becomes a new notice category or briefing fact.

| Feature | Computed from | Example wording |
|---|---|---|
| Evening wrap-up and tomorrow preview (replaces a "morning" briefing after 18:00) | Calendar tomorrow, Open-Meteo day 2 | "Nothing else tonight. Tomorrow starts with Team stand-up at 9:00, and rain is likely from noon: bring an umbrella." |
| Leave-by cue | Event start, a fixed travel buffer setting, rain (adds 5 min) | "Leave by 9:40 for Design review; it's raining." |
| Comfort trend prediction | Linear fit of the last 20 min of Thermo samples | "The room is warming and will pass 26 °C in about 15 min." |
| Free-time finder | Gaps between remaining events | "You have a free hour at 2 PM." |
| Daily room summary | SQLite time series (or the `dbstorage_tsstore` Brick) | "The room was above your range for 2 h this afternoon." |
| Presence-aware timing | Distance "object near" plus last touch | Hold a non-urgent cue until someone is likely at the desk; never claims a person |

- **Effort:** 1-2 days each. The evening wrap-up and the leave-by cue are the
  most demo-visible.

### P4. Voice in and out (uses existing Bricks)

- **Wake word:** the `arduino:keyword_spotting` Brick (installed; "Hey
  Arduino" model built in; needs a USB microphone or webcam mic on the dock).
  On a detection, the app opens the Ask sheet and starts listening. Runs on
  the CPU at low cost, and Edge Impulse can train a custom "Hey ContextCue".
- **Speech to text, two options:**
  - **Offline:** Vosk small English model (about 40 MB, runs faster than real
    time on Cortex-A53 class CPUs) with a grammar limited to the intent
    vocabulary. Needs a Python dependency in the app container.
  - **Cloud:** the `arduino:cloud_asr` Brick (installed). Better accuracy, but
    it sends audio off the board, so it is opt-in and labelled.
- **Text to speech:** the `speech` component is already wired and shows
  "no_tts_engine_selected". Piper (low voice) is the candidate for natural
  speech, with espeak-ng as the instant fallback. It goes to the Bluetooth
  speaker (plan section D).
- **Effort:** 3-4 days, with a mic. This is the biggest jump in "feels like
  AI". It must be measured against the CPU budget: wake word plus the LLM at
  the same time.

### P5. Sound awareness (optional)

- **What:** the `arduino:audio_classification` Brick detects a doorbell,
  alarm, knock or kettle and raises a visual cue. It is an accessibility
  feature that uses the same microphone as P4.
- **Effort:** 1 day with the pre-trained model; more with a custom Edge
  Impulse model.

### P6. Learning preferences (no model needed)

- **What:** "Too warm / Fine / Too cold" taps on comfort notices nudge the
  comfort range (bounded ±1 °C per day, shown in Settings, and resettable).
  "Not useful" on a notice type lowers its priority.
- **Why:** visible personalisation for the demo, with no AI risk. The model
  then words "you usually prefer…".

### Not recommended now

- **Tool calling and MCP (`arduino:mcp_client`):** the 0.8B model's tool
  selection is unreliable, and each tool round trip costs another 10-20 s.
  The P2 intent router gives the same result deterministically.
- **A bigger local model (2B and up):** prompt reading is already the
  bottleneck, and 2B is about 2.5x slower. Gemma 3 1B already failed G1
  (4/8, slower).
- **Candidate to benchmark later:** LFM2-700M, a hybrid built for edge CPUs.
  Benchmark it with `scripts/ai-eval.py` before any switch.
- **`arduino:cloud_llm` (Claude, GPT or Gemini):** possible as an opt-in
  "Cloud answers" mode for open-ended questions. It requires an API key and
  internet, and changes the privacy story. Keep it a stretch goal, labelled,
  and off by default.
- **`arduino:mood_detector`:** classifies typed text sentiment. ContextCue has
  little user text, so it adds no value.

## 4. Suggested order before demo day

1. P1 streaming and factual-first (removes the "timed out" experience).
2. P3 evening wrap-up and tomorrow preview, plus the leave-by cue (fixes the
   empty night briefing).
3. P2 intent router with keyboard and chip input.
4. P4 voice, if a USB microphone is available. Otherwise demo P2 by typing.
5. P6 and P5 as time allows.

## 5. Acceptance checks

- **Latency:** factual text appears in under 1 s and the first streamed token
  in under 8 s. AI text finishes in under 25 s at p90 on the board with the
  kiosk running.
- **Grounding:** 0 unsupported numbers or names over the evaluation set; the
  validator rejection rate is under 10%.
- **Intent router:** at least 90% accuracy on the phrasing set, and
  `unsupported` for at least 95% of off-topic questions.
- **Voice:** wake-to-answer in under 25 s; no false wakes over a 30-minute
  quiet run.
