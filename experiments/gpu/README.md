# Experiment E1: GPU acceleration for the on-board LLM

Run on 16 November 2026 on the Arduino UNO Q (Adreno 702 GPU, 4x Cortex-A53),
with the app and kiosk running. Plan: [AI_ACCELERATION_AND_VOICE_PLAN.md](../../docs/planning/AI_ACCELERATION_AND_VOICE_PLAN.md), section 2.

## Result

**The GPU cannot run the model.** The CPU stays the only usable path.

| Backend | Result |
|---|---|
| **CPU** (4 threads, kiosk running) | **Prompt reading 11.05 ± 0.02 tokens/s, writing 3.75 ± 0.04 tokens/s** (prompt 384 tokens, the size of ContextCue's requests; 64 tokens written; 3 runs) |
| **Vulkan** (Mesa Turnip, Adreno 702) | **Failed.** With the whole model on the GPU, the GPU locked up: the kernel logged `hangcheck detected gpu lockup ... offending task: llama-bench` and reset the GPU, and llama.cpp aborted with `vk::DeviceLostError`. The retry (one layer, with half-precision maths, integer dot products and cooperative matrices off) stopped with `Shared memory size too small for matrix multiplication` |
| **OpenCL** (Mesa Rusticl, "FD702") | **Refused.** llama.cpp reported `unsupported GPU 'FD702'`. Its OpenCL backend only supports specific Adreno chips with Qualcomm's own driver |

**Why Vulkan fails:** the Adreno 702 gives compute shaders 16 KB of shared
memory (`maxComputeSharedMemorySize = 16384`). llama.cpp's matrix
multiplication shaders need more, so the core operation of the model cannot
run on this GPU.

**Effect on the board:** the GPU reset did not stop the kiosk, the app or the
model server; at most the screen glitched briefly.

## Setup (reproducible)

- **Code:** llama.cpp commit `f4d8af608`, the exact build inside the
  `arduino:llm` Brick 0.13.2. It has no `b11950` tag; it is the parent of tag
  `b11951`.
- **Build:** a throwaway `debian:trixie` container with `/dev/dri` mapped
  (`build.sh`, `finish-build.sh`), with `GGML_VULKAN=ON`, `GGML_OPENCL=ON`
  and `GGML_NATIVE=ON`.
- **Benchmark:** `llama-bench` on the production model
  `Qwen3.5-0.8B-Q4_0.gguf` (`bench.sh`).
- **Logs:** `bench-kiosk.log` and `gpu-lockup-dmesg.log`.
- **Cleaned up:** the container, the image and the build files were removed
  from the board afterwards.

## Problems hit on the way (useful for anyone repeating it)

1. **apt in Docker:** apt 3.0 failed with "The package cache file is
   corrupted, it has the wrong hash" until locking was disabled
   (`Debug::NoLocking "1"`).
2. **GPU drivers:** Debian 13's own Mesa 25.0.7 does not recognise the
   Adreno 702 ("unsupported GPU id ... chip id 0xb207000200") and falls back
   to the `llvmpipe` software renderer. The board uses Mesa 25.2.6 from
   `trixie-backports`; the container needed the backports packages too.
3. **Memory:** compiling the 77 MB generated Vulkan shader file needed more
   than 1.6 GB of RAM and was killed by the kernel's out-of-memory killer
   (only the compiler; the app was untouched). It was then compiled alone at
   `-O0`, followed by one build job at a time.
4. **Disk:** the build filled the root disk (2.1 GB free before, 0 after)
   until cleanup. The app database stayed intact (`integrity_check: ok`).
   Anyone repeating this should keep at least 3 GB free.

## Conclusion for ContextCue

- GPU offload is not available on the UNO Q with llama.cpp today. That needs
  either a Vulkan matrix-multiplication path that fits in 16 KB of shared
  memory, or Qualcomm's OpenCL driver.
- On the CPU, a typical 360-token request is about 33 s of reading if
  nothing is cached; warm requests reuse the cached instructions (about 9 s
  of reading), plus about 8 s of writing at 3.75 tokens/s.
- A bigger or "thinking" model therefore cannot run on the board. Better
  answers have to come from tighter checks (done), code-computed facts, a
  fine-tuned model of the same size, or an opt-in cloud model.
