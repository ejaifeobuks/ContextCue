#pragma once

#include <stdint.h>
#include <stddef.h>

namespace contextcue {
constexpr char kSchemaVersion[] = "1.0";
constexpr char kFirmwareVersion[] = "0.1.0";
// Keep these in sync with sketch.yaml; the compile check uses that profile.
constexpr char kCoreVersion[] = "1.0.0";
constexpr char kBridgeVersion[] = "0.4.4";
constexpr char kModulinoVersion[] = "0.10.0";
constexpr char kHsVersion[] = "1.0.1";
constexpr char kShtVersion[] = "0.5.3";
constexpr uint32_t kThermoIntervalMs = 1000;
constexpr size_t kMaxJsonBytes = 224;
}  // namespace contextcue
