#!/bin/sh
# E1 build, step 1b: the generated mul_mm.comp.cpp (77 MB of embedded SPIR-V)
# needed >1.6 GB to compile at -O3 and was OOM-killed on the 3.6 GB board.
# It is only data, so compile it alone at -O0, then finish with one job.
set -eux
cd /work/llama.cpp/build
CMD=$(make -n -f ggml/src/ggml-vulkan/CMakeFiles/ggml-vulkan.dir/build.make \
  ggml/src/ggml-vulkan/CMakeFiles/ggml-vulkan.dir/mul_mm.comp.cpp.o 2>/dev/null \
  | grep -a -E "c\+\+ .*mul_mm.comp.cpp" | head -1)
CMD=$(echo "$CMD" | sed 's/ -O3 / -O0 -g0 /')
nice -n 19 sh -c "$CMD"
nice -n 19 cmake --build . --target llama-bench -j 1
ls -la bin/
echo BUILD_DONE
