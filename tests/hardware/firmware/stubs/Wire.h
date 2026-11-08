#pragma once

// Host stand-in for the Zephyr core's Wire (arduino::ZephyrI2C) on the Qwiic
// bus. Address reachability and fault modes are globals the tests switch.

#include <cstddef>
#include <cstdint>

#include <zephyr/kernel.h>

struct device { int id; };
inline device testQwiicDevice{4};
#define CONTEXTCUE_QWIIC_DEVICE (&testQwiicDevice)

inline bool distanceReachable = true;
// The Distance sensor ACKs its address but NACKs register transfers, as a
// confused or half-reset VL53L4CD does; this is what hung the real driver.
inline bool distanceRegisterFault = false;
// Something holds the bus: every transfer runs into the STM32 driver's
// 500 ms timeout and fails.
inline bool busHeld = false;
inline bool pixelsReachable = true;
inline bool buzzerReachable = true;
inline long testTransfers = 0;

namespace arduino {

class ZephyrI2C {
 public:
  explicit ZephyrI2C(const device*) {}
  virtual ~ZephyrI2C() = default;
  virtual void begin() {}
  virtual void setClock(uint32_t) {}
  virtual void beginTransmission(uint8_t address) {
    address_ = address;
    length_ = 0;
  }
  virtual size_t write(uint8_t) {
    ++length_;
    return 1;
  }
  virtual size_t write(const uint8_t*, size_t size) {
    length_ += size;
    return size;
  }
  virtual uint8_t endTransmission(bool) {
    ++testTransfers;
    if (busHeld) {
      testUptime += 500;
      return 1;
    }
    if (address_ == 0x29 && !distanceReachable) return 2;
    if (address_ == 0x29 && distanceRegisterFault && length_ > 0) return 3;
    if (address_ == 0x36 && !pixelsReachable) return 2;
    if (address_ == 0x1E && !buzzerReachable) return 2;
    return 0;
  }
  virtual uint8_t endTransmission() { return endTransmission(true); }
  virtual size_t requestFrom(uint8_t address, size_t length, bool) {
    ++testTransfers;
    if (busHeld) {
      testUptime += 500;
      return 0;
    }
    if (address == 0x29 && (!distanceReachable || distanceRegisterFault)) return 0;
    return length;
  }
  virtual size_t requestFrom(uint8_t address, size_t length) {
    return requestFrom(address, length, true);
  }

 private:
  uint8_t address_ = 0;
  size_t length_ = 0;
};

}  // namespace arduino

inline arduino::ZephyrI2C Wire1(&testQwiicDevice);
