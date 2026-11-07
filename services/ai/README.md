# AI service — planned, not implemented

Owns **RecommendationService** (SWE): the isolated, asynchronous, bounded LLM
worker (baseline APP-06 to APP-08).

- Receives immutable `ContextSnapshot` requests from ContextEngine and returns
  `ModelResponse` results that reference current evidence IDs.
- Validates schema, category, evidence and numeric facts; rejects obsolete
  results; produces labeled factual fallbacks; supports cancellation.
- Has no hardware authority and no silent cloud fallback.
- Uses the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

## Existing solution: Arduino `arduino:llm` Brick

Findings measured on the team's UNO Q on 6 November 2026 (Brick assets 0.13.2).

| Item | Finding |
|---|---|
| Runtime | llama.cpp container `ghcr.io/arduino/app-bricks/llamacpp-runner:0.13.2`, already on the board (203 MB) |
| API | OpenAI-compatible server at `http://127.0.0.1:9999/v1` |
| Limits | `mem_limit` 2500 MB, 4 threads, one loaded model, reasoning off, context `LLAMA_ARG_CTX_SIZE` (default 16384) |
| UNO Q models | `llamacpp:Qwen3.5-0.8B-Q4_0` (default, 507 MB), `llamacpp:gemma-3-1b-it-Q4_0` (722 MB). Larger models are VENTUNO Q only |
| Model storage | Downloaded by App Lab into `/var/lib/arduino-app-cli/models/llamacpp/`; none downloaded yet |
| Board budget | 3.6 GiB RAM (about 2.2 GiB available in use). Disk: 29.1 GiB eMMC, but models and Docker images live on the root partition, which has **2.5 GB free**; `/home/arduino` has 16 GB free |

Python API (`arduino.app_bricks.llm.LargeLanguageModel`):

```python
LargeLanguageModel(system_prompt="", temperature=0.7, max_tokens=512,
                   timeout=None, tools=None, model=None)
.chat(message) -> str          # blocks until finished
.chat_stream(message)          # yields tokens
.stop_stream()                 # interrupts a stream
.list_models()
```

Declare `arduino:llm` under `bricks:` in the app's `app.yaml`; App Lab then
starts the llama.cpp service and downloads the model.

## References

- On the board: `arduino-app-cli brick details arduino:llm`
- Brick catalogue and service definition:
  `/var/lib/arduino-app-cli/assets/0.13.2/bricks-list.yaml`, `models-list.yaml`,
  `services/arduino/llamacpp/service_compose.unoq.yaml`
- Brick source (inside the app base image):
  `docker run --rm --entrypoint sh ghcr.io/arduino/app-bricks/python-apps-base:0.13.2 -c "cat /usr/local/lib/python3.13/site-packages/arduino/app_bricks/llm/local_llm.py"`
- Reference app for this board:
  `/var/lib/arduino-app-cli/examples/inspirational/platform_unoq/edge-ai-assistant`
  (`web_ui` + `llm`; streams tokens over WebSocket; Stop calls `stop_stream()`)
- [Implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md), section A and "LLM" findings

## Implementation considerations

- **Never pass `tools`** and **never use `with_memory()`**: the model has no
  hardware authority, and each request must see only its own snapshot.
- Set `max_tokens=160` and context to about 4096 (input is capped at 2048
  tokens) to save memory.
- `timeout` is whole seconds; enforce the real deadline in our own async
  wrapper and run blocking calls in a worker thread so UI and controls stay
  responsive (APP-07).
- `chat()` returns free text, but `ModelResponse` needs category, text and
  evidence IDs. Either prompt for JSON and validate strictly, or call the
  llama.cpp endpoint directly with JSON-schema constrained output (not yet
  verified on this runner).
- One active request plus one coalesced waiting request (baseline §6); the
  service also loads only one model.
- `arduino:cloud_llm` exists but is an off-board contingency only: it must be
  an explicit, disclosed deployment decision, never a fallback.
- A factual fallback is required on failure but never counts as generation.

## Recommended starting point

1. Run the G1 benchmark (plan section A): a throwaway app with `arduino:llm`,
   about 30 representative snapshot prompts, cold/warm p50/p95 latency,
   tokens/s, peak memory, JSON validity, and the effect on sensing and UI.
2. Compare Qwen3.5-0.8B and Gemma 3 1B, and context 4096 versus 16384.
3. Record the chosen model, timeout and memory budget, then build the worker
   and validator around them.

Open questions: JSON-schema support on the runner, whether the UNO Q runner
uses the Hexagon DSP or only the CPU, and whether `stop_stream()` frees the
server immediately.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md).
