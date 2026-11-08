#pragma once

#include <Wire.h>
#include <stddef.h>
#include <stdint.h>

#include "BusGuard.h"

namespace contextcue {

// The Qwiic bus (the same Zephyr device as Wire1) with BusGuard applied to
// every transfer. All Modulino drivers use it through Modulino.begin(), so a
// guarded section can stop the Distance driver's unbounded retry loop.
// Outside a guarded section it behaves exactly like Wire1.
class QwiicBus : public arduino::ZephyrI2C {
 public:
  explicit QwiicBus(const struct device* device) : arduino::ZephyrI2C(device) {}

  uint8_t endTransmission(bool stopBit) override;
  uint8_t endTransmission() override;
  size_t requestFrom(uint8_t address, size_t length, bool stopBit) override;
  size_t requestFrom(uint8_t address, size_t length) override;

  BusGuard& guard() { return guard_; }

 private:
  BusGuard guard_;
};

QwiicBus& qwiicBus();

// Arms the bus guard for one scope; tripped() reports whether it gave up.
class GuardedSection {
 public:
  GuardedSection() { qwiicBus().guard().arm(); }
  ~GuardedSection() { finish(); }
  bool finish() {
    if (done_) return tripped_;
    done_ = true;
    tripped_ = qwiicBus().guard().disarm();
    return tripped_;
  }

 private:
  bool done_ = false;
  bool tripped_ = false;
};

}  // namespace contextcue
