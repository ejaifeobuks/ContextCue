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
  const char* quality = "unavailable";
  const char* error = "not_sampled";
};

// Hardware acquisition only. Comfort policy and calendar time belong on Linux.
// Arduino_Modulino owns Thermo detection and the underlying sensor drivers.
class SensorController {
 public:
  void begin() {
    Modulino.begin(Wire1);
    initialized_ = thermo_.begin();
  }

  void readTemperatureHumidity(uint64_t now) {
    if (attempted_ && now - lastAttempt_ < kThermoIntervalMs) return;
    attempted_ = true;
    lastAttempt_ = now;
    reading_.sequence++;
    // Timestamp the start of acquisition, conservatively accounting for driver
    // conversion delays. Failed acquisitions have their own sequence and time.
    reading_.observedUptimeMs = now;
    reading_.temperature = NAN;
    reading_.humidity = NAN;

    // begin() is the library's reachability check. Repeat it once per sample
    // so a disconnected Thermo cannot leave its previous values marked valid.
    const bool wasReady = initialized_;
    initialized_ = thermo_.begin();
    if (!initialized_) {
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
    reading_.temperature = temperature;
    reading_.humidity = humidity;
    reading_.quality = "valid";
    reading_.error = nullptr;
  }

  const ThermoReading& reading() const { return reading_; }
  bool available() const { return reading_.error == nullptr; }
  const char* libraryName() const {
    return initialized_ ? "modulino" : "none";
  }

  bool validateReading(float temperature, float humidity) const {
    return isfinite(temperature) && isfinite(humidity) &&
           temperature >= -40 && temperature <= 125 &&
           humidity >= 0 && humidity <= 100;
  }

  void reportSensorFailure(const char* quality, const char* error) {
    reading_.quality = quality;
    reading_.error = error;
    reading_.temperature = NAN;
    reading_.humidity = NAN;
    initialized_ = false;
  }

 private:
  ModulinoThermo thermo_;
  bool initialized_ = false;
  ThermoReading reading_;
  bool attempted_ = false;
  uint64_t lastAttempt_ = 0;
};

}  // namespace contextcue
