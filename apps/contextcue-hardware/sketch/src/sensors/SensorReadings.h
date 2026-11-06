#pragma once

#include <math.h>
#include <stdint.h>

namespace contextcue {

// Completed acquisition records. SensorController writes them on the loop
// thread; the published copies are what Bridge RPC callbacks serialize.

struct ThermoReading {
  uint64_t sequence = 0;
  uint64_t observedUptimeMs = 0;
  float temperature = NAN;
  float humidity = NAN;
  bool available = false;
  const char* quality = "unavailable";
  const char* error = "not_sampled";
};

struct LightReading {
  uint64_t sequence = 0;
  uint64_t observedUptimeMs = 0;
  float ambientLightLux = NAN;
  float normalizedLevel = NAN;
  bool available = false;
  const char* quality = "unavailable";
  const char* error = "light_not_found";
};

struct DistanceReading {
  uint64_t sequence = 0;
  uint64_t observedUptimeMs = 0;
  float millimeters = NAN;
  bool available = false;
  const char* quality = "unavailable";
  const char* error = "distance_not_found";
};

}  // namespace contextcue
