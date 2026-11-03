#pragma once

#include <Arduino_Modulino.h>
#include <math.h>

#include "Config.h"

namespace contextcue {

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
  float ambientLevel = NAN;
  bool available = false;
  const char* quality = "unavailable";
  const char* error = "not_implemented";
};

struct DistanceReading {
  uint64_t sequence = 0;
  uint64_t observedUptimeMs = 0;
  float millimeters = NAN;
  bool available = false;
  const char* quality = "unavailable";
  const char* error = "not_implemented";
};

// Hardware acquisition only. Comfort policy and calendar time belong on Linux.
// Arduino_Modulino owns Thermo detection and the underlying sensor drivers.
class SensorController {
 public:
  void begin() {
    Modulino.begin(Wire1);
    thermoInitialized_ = thermo_.begin();
    thermoReading_.available = thermoInitialized_;
    thermoReading_.quality = "unavailable";
    thermoReading_.error = thermoInitialized_ ? "not_sampled" : "thermo_not_found";
  }

  void readThermoIfDue(uint64_t now) {
    if (attempted_ && now - lastAttempt_ < kThermoIntervalMs) return;
    attempted_ = true;
    lastAttempt_ = now;
    thermoReading_.sequence++;
    // Timestamp the start of acquisition, conservatively accounting for driver
    // conversion delays. Failed acquisitions have their own sequence and time.
    thermoReading_.observedUptimeMs = now;
    thermoReading_.temperature = NAN;
    thermoReading_.humidity = NAN;

    // begin() is the library's reachability check. Repeat it once per sample
    // so a disconnected Thermo cannot leave its previous values marked valid.
    const bool wasReady = thermoInitialized_;
    thermoInitialized_ = thermo_.begin();
    if (!thermoInitialized_) {
      reportSensorFailure("unavailable", wasReady ? "thermo_disconnected" : "thermo_not_found");
      return;
    }

    const float temperature = thermo_.getTemperature();
    const float humidity = thermo_.getHumidity();
    if (!isfinite(temperature) || !isfinite(humidity)) {
      reportSensorFailure("invalid", "thermo_read_failed");
      return;
    }
    if (!validateReading(temperature, humidity)) {
      reportSensorFailure("invalid", "thermo_out_of_range");
      return;
    }
    thermoReading_.temperature = temperature;
    thermoReading_.humidity = humidity;
    thermoReading_.available = true;
    thermoReading_.quality = "valid";
    thermoReading_.error = nullptr;
  }

  // Compatibility wrapper for the first Thermo-only firmware milestone.
  void readTemperatureHumidity(uint64_t now) { readThermoIfDue(now); }

  const ThermoReading& reading() const { return thermoReading_; }
  const ThermoReading& thermoReading() const { return thermoReading_; }
  const LightReading& lightReading() const { return lightReading_; }
  const DistanceReading& distanceReading() const { return distanceReading_; }
  bool available() const { return thermoReading_.available; }
  const char* libraryName() const {
    return thermoInitialized_ ? "modulino" : "none";
  }

  bool validateReading(float temperature, float humidity) const {
    return isfinite(temperature) && isfinite(humidity) &&
           temperature >= -40 && temperature <= 125 &&
           humidity >= 0 && humidity <= 100;
  }

  void reportSensorFailure(const char* quality, const char* error) {
    thermoReading_.quality = quality;
    thermoReading_.error = error;
    thermoReading_.temperature = NAN;
    thermoReading_.humidity = NAN;
    thermoReading_.available = false;
    thermoInitialized_ = false;
  }

 private:
  ModulinoThermo thermo_;
  bool thermoInitialized_ = false;
  ThermoReading thermoReading_;
  LightReading lightReading_;
  DistanceReading distanceReading_;
  bool attempted_ = false;
  uint64_t lastAttempt_ = 0;
};

}  // namespace contextcue
