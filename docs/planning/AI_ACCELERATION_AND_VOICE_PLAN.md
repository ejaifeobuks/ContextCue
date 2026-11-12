# ContextCue AI Acceleration and Voice Plan

Status: investigation and plan, 11 November 2026. Extends
[AI_EXPANSION_PLAN.md](AI_EXPANSION_PLAN.md). Nothing here is implemented yet.

## 1. What the board actually has (checked on the board)

| Part | Finding | Usable for AI? |
|---|---|---|
| CPU | 4x Cortex-A53, up to 2.0 GHz (QRB2210 / QCM2290) | Yes, the only path in use today |
| RAM | 3.6 GB total, about 1.8 GB available with the app and kiosk running | Limits model size to about 1.5 GB |
| GPU | Adreno 702, up to 845 MHz. Mesa drivers work: Vulkan 1.4 ("Turnip Adreno (TM) 702") and OpenCL through Rusticl ("FD702") | Possibly. Not yet measured with llama.cpp |
| DSP | Only the audio DSP (`adsp`) runs; `/dev/fastrpc-adsp` is root-only. There is no compute DSP or HTP | No |
| NPU | None on the UNO Q. The `llamacpp-npu-runner` image is for boards with a Hexagon NPU, such as the VENTUNO Q (Dragonwing IQ8) | No |
| Audio | Board codec card "Arduino-Imola-HPH-LOUT" (headphone and line out). No microphone attached | Output yes; input needs a USB microphone |

The `arduino:llm` Brick (llamacpp-runner 0.13.2) ships a CPU-only llama.cpp
build (`/opt/pkg-cpu`, started with `--device none`), and its container does
not get `/dev/dri`. **Using the GPU means running our own llama.cpp build in
a custom Brick, not changing a setting.**

## 2. Will the GPU help? What to expect

- **Generation** (2-4 tokens/s now) is limited mostly by memory bandwidth. The
  GPU shares the same LPDDR4X memory, so expect little or no gain there.
- **Prompt reading** (7-11 tokens/s now, the bigger share of the 12-19 s) is
  limited by arithmetic. It is the part a GPU can speed up.
- **Risks:**
  - llama.cpp's OpenCL backend is tuned for Adreno 7xx/8xx with Qualcomm's own
    driver. Rusticl on an Adreno 702 is untested.
  - The Vulkan backend has a history of poor results on mobile Adreno.
  - The GPU also draws the kiosk, so Chromium and the model would compete.
- **Decision rule:** adopt the GPU only if it measurably cuts total request
  time on the board with the kiosk running. Otherwise stay on the CPU.

### Experiment E1: GPU benchmark (needs approval)

It runs in a throwaway Docker container on the board, so it needs no sudo and
leaves the system untouched:

1. Build upstream llama.cpp with `GGML_VULKAN=ON` and `GGML_OPENCL=ON` in a
   `debian:trixie` container that has `/dev/dri` mapped.
2. Run `llama-bench` on the existing `Qwen3.5-0.8B-Q4_0.gguf` three ways:
   CPU, Vulkan and OpenCL. Measure prompt reading (pp512) and generation
   (tg64).
3. Repeat the best result with the kiosk running.

The download and build were blocked by the permission check in this session
(it fetches code from GitHub and compiles it on the board). Run it after
approval. It takes about 1 hour of build time and about 1 GB of disk, which
is deleted afterwards.

**If the GPU wins:** package a custom Brick `contextcue-llm-gpu` in
`apps/contextcue/bricks/` (App Lab supports app-local Bricks with
`brick_compose.yaml`). It exposes the same `/v1` API, so the app needs only a
URL change.

## 3. Better responses without more speed

Quantising makes a model smaller and faster, not smarter. We already use
Q4_0, which is the fastest format on ARM CPUs because llama.cpp repacks it.
The levers that actually improve answers are below.

| Lever | Expected effect | Cost |
|---|---|---|
| **A. Fix the known wording faults** in the prompt and fact snapshot ("in Huntsville, Alabama" attached to an event, "1.0%", "19:00", "Morning briefing" in the evening) | Removes the most visible "poor" answers | 0.5 day, no speed cost |
| **B. Fine-tune the 0.8B model for our one job.** Generate about 2,000 snapshot-to-notice pairs with a large model, keep only those our validator accepts, then train a LoRA on the laptop or Colab, merge it, and export to GGUF Q4_0 | The biggest quality gain at the same speed. Small models get much better at one narrow task | 2-3 days. Uses the existing validator and `scripts/ai-eval.py` |
| **C. Try other small models** with the existing Brick (no build needed): Qwen3.5-0.8B at Q8_0, and LFM2-700M / LFM2-1.2B | May beat Qwen at the same or similar speed | 1 day. Download through the Brick's model downloader, score with `scripts/ai-eval.py` |
| **D. A bigger model (about 1.5-2B)** | Better wording, but 2-2.5x slower on the CPU (25-45 s) | Only if E1 shows a large GPU gain |
| **E. Prompt caching:** keep the system prompt and fixed text as an identical prefix so llama.cpp reuses its cache | Shorter prompt reading on repeat requests | 0.5 day to verify and measure |

**Recommended order:** A, then C, then B. D only after E1.

### Results (11-12 November 2026)

- **A, wording fixes: done** (prompt version `contextcue-prompt-3`).
  - The model now sees 12-hour times, whole percentages, no weather place
    name, and "briefing" instead of "morning".
  - The UI names a briefing by the time of day.
  - Board evaluation: 8/8 valid.
- **C, other models: done; keep Qwen3.5-0.8B Q4_0.** Board evaluation, with
  the same 8 app prompts and the kiosk running:

| Model (Hugging Face GGUF) | Valid | p50 / max | Notes |
|---|---|---|---|
| **Qwen3.5-0.8B Q4_0 (current)** | **8/8** | **21 s / 31 s** | Stiff; sometimes omits the event name |
| Qwen3.5-0.8B Q8_0 | 7/8 | 23.5 s / 33 s | Names events better; invented a "20-25 °C" range; said "no rain" with 70% later |
| LFM2.5-1.2B-Instruct (QAD Q4_0) | 6/8 | 28 s / 71 s | Invented "50% chance of rain"; umbrella advice at 10% |
| LFM2-700M Q4_0 | 3/8 | 27 s / 53 s | Ignored the length limit, broken JSON, invented numbers |

- **Next for quality:** B (fine-tuning), since no drop-in model beats the
  current one.

## 4. Edge Impulse: where it fits

Edge Impulse trains small audio, sensor and vision models. It does not train
or host language models, so it cannot improve the LLM directly. It can take
work away from the LLM and enable voice:

1. **Custom wake word "Hey ContextCue"** for the `arduino:keyword_spotting`
   Brick (it accepts Edge Impulse models). Record about 50 samples per speaker
   plus background noise.
2. **Fixed voice commands as keywords**, for example "next meeting", "weather",
   "room", "dismiss" and "repeat". This gives instant, offline commands with
   no speech-to-text and no LLM wait. It is the fastest route to a working
   voice demo.
3. **Sound awareness** (doorbell, alarm, knock) with the
   `arduino:audio_classification` Brick.
4. **Optional:** a desk-presence classifier trained on Distance and Light
   readings, to time spoken notices better. It never identifies a person.

Deploy with the "Linux Arduino UNO Q" target, or "UNO Q (GPU)" to test GPU
inference, then import into App Lab. Known problems: custom keyword models
sometimes need a manual `app.yaml` edit, and imports have failed on older App
Lab versions.

## 5. Voice service plan

### Pipeline

```
USB mic → wake word (keyword_spotting, Edge Impulse) → record until silence (VAD)
       → speech to text (fixed commands via Edge Impulse, or free speech via STT)
       → intent router (AI_EXPANSION_PLAN P2) → code computes the answer
       → wording (template now, LLM optional) → text to speech → speaker
```

### Component choices

| Stage | Offline choice (default) | Opt-in cloud choice |
|---|---|---|
| Wake word | `arduino:keyword_spotting` with a custom Edge Impulse model | none |
| Commands | Edge Impulse keyword model (section 4, item 2) | none |
| Free speech to text | Moonshine Tiny (fastest published edge results) or Vosk small English with a grammar limited to the intent words. whisper.cpp tiny.en is the fallback; it is likely too slow on an A53 | `arduino:cloud_asr` (OpenAI or Google), labelled "Cloud" |
| Text to speech | Piper (low-quality voice) for natural speech, espeak-ng as an instant fallback | none needed |
| Output | Board line out or a USB speaker. Bluetooth later (the original plan section D) | none |

### CPU budget rules

- Only one heavy model runs at a time. Pause or cancel LLM requests while
  listening or transcribing, and speak the factual text when the LLM is busy.
- Spoken answers use the template text first. AI wording is optional for
  voice, because 12-19 s is too slow for a reply.

### V0 status: implemented (12 November 2026)

- **Engine:** `contextcue_app/speech.py` (`PiperSpeech`) uses Piper 1.8.0 with
  the `en_US-lessac-medium` voice. The voice is pinned by SHA-256 and stored
  in the app's `data/voices`.
- **Output:** played through Arduino's `Speaker("pipewire")`, so any USB,
  Bluetooth or built-in output chosen on the board is used.
- **Measured on the board, kiosk running:**
  - Voice load: 14 s.
  - Synthesis: 4.2 s for 7.4 s of speech.
  - First sentence ready: 1.7 s.
  - Stop takes effect in 0.3 s.
- **Unchanged:** the existing notification policy decides when to speak
  (Audio setting, mute, Focus, quiet hours, once per notice, Play button).

### Stages

| Stage | What works at the end | Needs | Effort |
|---|---|---|---|
| **V0** | Notices are spoken aloud (Piper or espeak-ng), with mute, volume and quiet hours respected | A speaker on line out or USB | 1-2 days |
| **V1** | "Hey Arduino" (built in) or a custom "Hey ContextCue" wake word opens the Ask sheet | USB microphone, Edge Impulse project | 1-2 days |
| **V2** | About 6 spoken commands answered instantly (Edge Impulse keywords) | V1 | 2 days |
| **V3** | Free questions through offline STT and the intent router (P2) | V1, plus P2 from AI_EXPANSION_PLAN | 3-4 days |
| **V4** | Optional Cloud ASR toggle, labelled, off by default | API key, internet | 1 day |

### Experiment E2: speech benchmark (needs approval)

On the board, in a throwaway container, measure:

- Piper's real-time factor (it must be below 1, faster than real time)
- Moonshine Tiny and Vosk latency on 5 recorded test phrases
- both of the above while the kiosk runs

The tools are downloaded from their official releases, like E1.

## 6. Open questions

- Which microphone and speaker? A USB webcam microphone works with the audio
  Bricks. Check whether App Lab maps `/dev/snd` into the app container or only
  into the audio Bricks.
- Is the GPU free enough with the kiosk drawing at 60 Hz? E1 answers this.
- Fine-tuning data: generate it with a cloud model once, offline from the
  board. The board itself stays offline-capable.
