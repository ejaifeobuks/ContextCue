#!/bin/sh
# E1 GPU benchmark, step 2: CPU vs Vulkan vs OpenCL on the production model,
# with llama-bench from the same llama.cpp commit as the arduino:llm Brick.
#   pp = prompt reading (tokens/s), tg = writing (tokens/s)
# Prompt 384 tokens: ContextCue's requests are 355-410 tokens.
# Usage (inside the e1-gpu container): sh bench.sh LABEL
LABEL=${1:-run}
MODEL=/models/unsloth/Qwen3.5-0.8B-GGUF/Qwen3.5-0.8B-Q4_0.gguf
BENCH=/work/llama.cpp/build/bin/llama-bench
export RUSTICL_ENABLE=freedreno
echo "=== $LABEL $(date -u +%FT%TZ)"
echo "--- devices"
vulkaninfo --summary 2>&1 | grep -E "deviceName|driverName|apiVersion" | head -6
clinfo -l 2>&1 | head -6
$BENCH --list-devices 2>&1 | tail -6
for dev in none $($BENCH --list-devices 2>/dev/null | awk -F: '/^ *(Vulkan|GPUOpenCL)[0-9]*:/{gsub(/ /,"",$1); print $1}'); do
  ngl=99; [ "$dev" = none ] && ngl=0
  echo "--- device $dev (ngl $ngl)"
  timeout 900 $BENCH -m "$MODEL" -p 384 -n 64 -r 3 -t 4 -ngl $ngl -dev "$dev" -o md 2>&1 | grep -v -E "^(load|llama|ggml_|print_info)" | tail -8
done
echo "=== done $LABEL"
