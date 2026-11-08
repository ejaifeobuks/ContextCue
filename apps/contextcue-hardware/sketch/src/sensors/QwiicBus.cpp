#include "QwiicBus.h"

#include "../Uptime.h"

// zephyr,user i2cs = <&i2c2>, <&i2c4>, <&i2c3> on the UNO Q: index 1 is Wire1,
// the Qwiic connector. The host tests define this to their stub device.
#ifndef CONTEXTCUE_QWIIC_DEVICE
#define CONTEXTCUE_QWIIC_DEVICE DEVICE_DT_GET(DT_PHANDLE_BY_IDX(DT_PATH(zephyr_user), i2cs, 1))
#endif

namespace contextcue {
namespace {

QwiicBus bus(CONTEXTCUE_QWIIC_DEVICE);

uint32_t elapsedSince(uint64_t started) {
  return static_cast<uint32_t>(uptimeMs() - started);
}

}  // namespace

QwiicBus& qwiicBus() { return bus; }

uint8_t QwiicBus::endTransmission(bool stopBit) {
  if (guard_.shortCircuit()) return 0;
  const uint64_t started = uptimeMs();
  const uint8_t result = arduino::ZephyrI2C::endTransmission(stopBit);
  guard_.noteDuration(elapsedSince(started));
  return guard_.filterEnd(result);
}

uint8_t QwiicBus::endTransmission() { return endTransmission(true); }

size_t QwiicBus::requestFrom(uint8_t address, size_t length, bool stopBit) {
  if (guard_.shortCircuit()) return 0;
  const uint64_t started = uptimeMs();
  const size_t received = arduino::ZephyrI2C::requestFrom(address, length, stopBit);
  guard_.noteDuration(elapsedSince(started));
  return received;
}

size_t QwiicBus::requestFrom(uint8_t address, size_t length) {
  return requestFrom(address, length, true);
}

}  // namespace contextcue
