#pragma once

#include <stdint.h>
#include <stddef.h>

namespace contextcue {
constexpr char kSchemaVersion[] = "1.0";
constexpr char kFirmwareVersion[] = "0.1.4";
// Keep these in sync with sketch.yaml; the compile check uses that profile.
constexpr char kCoreVersion[] = "1.0.0";
constexpr char kBridgeVersion[] = "0.4.4";
constexpr char kModulinoVersion[] = "0.10.0";
constexpr char kHsVersion[] = "1.0.1";
constexpr char kShtVersion[] = "0.5.3";
constexpr uint32_t kThermoIntervalMs = 1000;
constexpr uint32_t kLightIntervalMs = 200;
constexpr uint32_t kDistanceIntervalMs = 100;
// VL53L4CD/VL53L4ED default address, probed by ModulinoDistance::begin().
constexpr uint8_t kDistanceI2cAddress = 0x29;
constexpr float kLightMaximumLux = 1000000.0f;
constexpr int kDistanceMaximumMm = 4000;
constexpr size_t kMaxJsonBytes = 224;
}  // namespace contextcue
