#include "BusGuard.h"

#include "../Config.h"

namespace contextcue {

void BusGuard::arm() {
  armed_ = true;
  tripped_ = false;
  streak_ = 0;
}

bool BusGuard::disarm() {
  const bool tripped = tripped_;
  armed_ = false;
  tripped_ = false;
  streak_ = 0;
  return tripped;
}

uint8_t BusGuard::filterEnd(uint8_t result) {
  if (!armed_) return result;
  if (tripped_) return 0;
  if (result == 0) {
    streak_ = 0;
    return 0;
  }
  if (++streak_ < kMaxBusRetries) return result;
  // Give up: pretend success so the driver's unbounded retry loop exits.
  tripped_ = true;
  ++trips_;
  return 0;
}

void BusGuard::noteDuration(uint32_t milliseconds) {
  if (milliseconds >= kSlowTransferMs) slow_ = true;
}

bool BusGuard::consumeSlow() {
  const bool slow = slow_;
  slow_ = false;
  return slow;
}

}  // namespace contextcue
