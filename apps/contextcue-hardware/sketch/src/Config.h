#pragma once

#include <stdint.h>
#include <stddef.h>

namespace contextcue {
constexpr char kSchemaVersion[] = "1.0";
constexpr char kFirmwareVersion[] = "0.2.0";
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

// Outputs (baseline §5): MCU-enforced bounds, independent of Linux checks.
// Modulino Pixels/Buzzer 7-bit addresses (the library's 0x6C/0x3C halved).
constexpr uint8_t kPixelsI2cAddress = 0x36;
constexpr uint8_t kBuzzerI2cAddress = 0x1E;
constexpr uint32_t kOutputLeaseMs = 3000;     // silence/off after this without renewal
constexpr uint32_t kOutputProbeIntervalMs = 1000;
constexpr int kMaxCommandTtlMs = 2000;
constexpr int kMaxToneDurationMs = 250;
constexpr int kMinToneFrequencyHz = 200;
constexpr int kMaxToneFrequencyHz = 5000;
constexpr int kMaxBrightnessPercent = 100;
constexpr size_t kMaxIdLength = 32;           // session and command IDs
constexpr size_t kRecentCommandIds = 16;      // duplicate detection per session
constexpr size_t kOutputQueueLength = 8;
}  // namespace contextcue
