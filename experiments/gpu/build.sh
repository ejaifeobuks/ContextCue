#!/bin/sh
# E1 GPU benchmark, step 1: build llama.cpp b11950 (the version inside the
# arduino:llm Brick 0.13.2) with the Vulkan and OpenCL backends, inside a
# throwaway debian:trixie container. Nothing is installed on the board.
#
# Started from the board host by run-e1.sh; logs to /work/build.log.
set -eux
export DEBIAN_FRONTEND=noninteractive
# apt 3.0 inside Docker on this board reports "The package cache file is
# corrupted, it has the wrong hash" whenever it takes its locks; with locking
# off it works. Safe here: one apt process in a throwaway container.
printf 'Debug::NoLocking "1";\n' > /etc/apt/apt.conf.d/99nolock
for i in 1 2 3; do
  rm -rf /var/lib/apt/lists/* /var/cache/apt/*.bin
  apt-get -o Acquire::Retries=5 update && apt-get install -y --no-install-recommends \
    build-essential cmake git ca-certificates pkg-config \
    libvulkan-dev glslc mesa-vulkan-drivers vulkan-tools spirv-headers \
    ocl-icd-opencl-dev opencl-headers mesa-opencl-icd clinfo && break
  sleep 10
done
command -v cmake && command -v glslc && command -v clinfo || { echo "PACKAGE_INSTALL_FAILED"; exit 1; }
cd /work
# The Brick reports build b11950, commit f4d8af608; there is no b11950 tag,
# so take b11951 and step back to its parent when that is f4d8af608.
[ -d llama.cpp ] || git clone --depth 2 --branch b11951 https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
case "$(git rev-parse HEAD~1)" in f4d8af608*) git checkout -q HEAD~1 ;; esac
git log -1 --format='commit %H %cd'
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=OFF \
  -DGGML_VULKAN=ON -DGGML_OPENCL=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
nice -n 19 cmake --build build --target llama-bench -j 2
ls -la build/bin/
echo BUILD_DONE
