#pragma once

#include <stdint.h>

namespace contextcue {

// Zephyr's 64-bit kernel uptime; safe to read from the loop and RPC threads.
uint64_t uptimeMs();

}  // namespace contextcue
