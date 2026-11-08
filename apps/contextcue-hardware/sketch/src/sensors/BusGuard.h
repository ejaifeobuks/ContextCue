#pragma once

#include <stdint.h>

namespace contextcue {

// Keeps one failing I2C device from freezing the MCU.
//
// The STM32duino VL53L4CD/VL53L4ED drivers (Distance) retry a register access
// with `do { ... } while (endTransmission(false) != 0);` and no limit. One
// device that stops acknowledging therefore never returns from loop(); and
// because every Bridge RPC is registered with provide_safe (served between
// loop() iterations), all RPCs then time out and the Linux side reports
// mcu_unresponsive. See docs/planning/IMPLEMENTATION_PLAN.md, section B.
//
// Inside an armed section, BusGuard lets a transfer fail kMaxBusRetries times
// in a row and then "trips": it reports success to break the driver's retry
// loop, and every later transfer in the section is skipped without touching
// the bus. The caller checks tripped() and discards whatever the driver read.
// Outside a section it changes nothing.
//
// It also notes transfers slow enough to have hit the driver's own timeout
// (a held bus), so the caller can back off instead of stalling every loop.
class BusGuard {
 public:
  void arm();
  // Ends the section; returns true if it tripped.
  bool disarm();
  bool armed() const { return armed_; }
  bool tripped() const { return tripped_; }
  // True once tripped in the current section: skip all bus I/O.
  bool shortCircuit() const { return armed_ && tripped_; }

  // Result of a real endTransmission; returns what to hand to the driver.
  uint8_t filterEnd(uint8_t result);
  // Duration of a real transfer, in milliseconds.
  void noteDuration(uint32_t milliseconds);
  // True if a slow transfer happened since the last call.
  bool consumeSlow();
  // While paused, callers skip optional bus traffic (sensor sampling, output
  // probes) so loop() and the Bridge RPCs it serves stay responsive.
  void pauseUntil(uint64_t until) { pausedUntil_ = until; }
  bool paused(uint64_t now) const { return now < pausedUntil_; }

  uint32_t trips() const { return trips_; }

 private:
  bool armed_ = false;
  bool tripped_ = false;
  bool slow_ = false;
  uint8_t streak_ = 0;
  uint32_t trips_ = 0;
  uint64_t pausedUntil_ = 0;
};

}  // namespace contextcue
