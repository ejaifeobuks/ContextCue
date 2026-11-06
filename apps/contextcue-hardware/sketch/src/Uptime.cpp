#include "Uptime.h"

#include <zephyr/kernel.h>

namespace contextcue {

uint64_t uptimeMs() {
  return static_cast<uint64_t>(k_uptime_get());
}

}  // namespace contextcue
