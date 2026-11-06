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

// Hardware acquisition only. Comfort policy and calendar time belong on Linux.
// Arduino_Modulino owns Thermo detection and the underlying sensor drivers.
class SensorController {
 public:
  void begin() {
    Modulino.begin(Wire1);
    thermoInitialized_ = thermo_.begin();
    lightInitialized_ = light_.begin();
    distanceInitialized_ = distance_.begin();
    thermoReading_.available = thermoInitialized_;
    thermoReading_.quality = "unavailable";
    thermoReading_.error = thermoInitialized_ ? "not_sampled" : "thermo_not_found";
    lightReading_.available = false;
    lightReading_.quality = "unavailable";
    lightReading_.error = lightInitialized_ ? "not_sampled" : "light_not_found";
    distanceReading_.available = false;
    distanceReading_.quality = "unavailable";
    distanceReading_.error =
        distanceInitialized_ ? "not_sampled" : "distance_not_found";
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

  void readLightIfDue(uint64_t now) {
    if (lightAttempted_ && now - lastLightAttempt_ < kLightIntervalMs) return;
    lightAttempted_ = true;
    lastLightAttempt_ = now;
    lightReading_.sequence++;
    lightReading_.observedUptimeMs = now;
    lightReading_.ambientLightLux = NAN;
    lightReading_.normalizedLevel = NAN;

    const bool wasReady = lightInitialized_;
    lightInitialized_ = light_.begin();
    if (!lightInitialized_) {
      reportLightFailure("unavailable",
                         wasReady ? "light_disconnected" : "light_not_found");
      return;
    }
    if (!light_.update()) {
      reportLightFailure("invalid", "light_read_failed");
      return;
    }

    const float lux = static_cast<float>(light_.getAL());
    if (!validateLightReading(lux)) {
      reportLightFailure("invalid", "light_out_of_range");
      return;
    }
    lightReading_.ambientLightLux = lux;
    lightReading_.normalizedLevel = normalizeLight(lux);
    lightReading_.available = true;
    lightReading_.quality = "valid";
    lightReading_.error = nullptr;
  }

  bool setLightCalibration(float darkReference, float brightReference) {
    if (!isfinite(darkReference) || !isfinite(brightReference) ||
        darkReference < 0 || brightReference <= darkReference) {
      lightCalibrationValid_ = false;
      reportLightFailure("invalid", "light_calibration_invalid");
      return false;
    }
    darkReference_ = darkReference;
    brightReference_ = brightReference;
    lightCalibrationValid_ = true;
    return true;
  }

  void readDistanceIfDue(uint64_t now) {
    if (distanceAttempted_ &&
        now - lastDistanceAttempt_ < kDistanceIntervalMs) {
      return;
    }
    distanceAttempted_ = true;
    lastDistanceAttempt_ = now;
    distanceReading_.sequence++;
    distanceReading_.observedUptimeMs = now;
    distanceReading_.millimeters = NAN;

    const bool wasReady = distanceInitialized_;
    distanceInitialized_ = distance_.begin();
    if (!distanceInitialized_) {
      reportDistanceFailure(
          "unavailable",
          wasReady ? "distance_disconnected" : "distance_not_found", true);
      return;
    }
    if (!distance_.available()) {
      reportDistanceFailure("unavailable", "distance_timeout");
      return;
    }

    const int millimeters = distance_.get();
    if (!validateDistanceReading(millimeters)) {
      reportDistanceFailure("invalid", "distance_out_of_range");
      return;
    }
    distanceReading_.millimeters = static_cast<float>(millimeters);
    distanceReading_.available = true;
    distanceReading_.quality = "valid";
    distanceReading_.error = nullptr;
  }

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

  bool validateLightReading(float lux) const {
    return isfinite(lux) && lux >= 0 && lux <= kLightMaximumLux;
  }

  float normalizeLight(float lux) const {
    if (!lightCalibrationValid_) return NAN;
    const float normalized = (lux - darkReference_) /
                            (brightReference_ - darkReference_);
    return normalized < 0 ? 0 : (normalized > 1 ? 1 : normalized);
  }

  bool validateDistanceReading(int millimeters) const {
    return millimeters >= 0 && millimeters <= kDistanceMaximumMm;
  }

  void reportSensorFailure(const char* quality, const char* error) {
    thermoReading_.quality = quality;
    thermoReading_.error = error;
    thermoReading_.temperature = NAN;
    thermoReading_.humidity = NAN;
    thermoReading_.available = false;
    thermoInitialized_ = false;
  }

  void reportLightFailure(const char* quality, const char* error) {
    lightReading_.quality = quality;
    lightReading_.error = error;
    lightReading_.ambientLightLux = NAN;
    lightReading_.normalizedLevel = NAN;
    lightReading_.available = false;
    lightInitialized_ = false;
  }

  void reportDistanceFailure(const char* quality, const char* error,
                             bool disconnected = false) {
    distanceReading_.quality = quality;
    distanceReading_.error = error;
    distanceReading_.millimeters = NAN;
    distanceReading_.available = false;
    distanceInitialized_ = !disconnected;
  }

 private:
  ModulinoThermo thermo_;
  ModulinoLight light_;
  ModulinoDistance distance_;
  bool thermoInitialized_ = false;
  bool lightInitialized_ = false;
  bool distanceInitialized_ = false;
  bool lightCalibrationValid_ = false;
  float darkReference_ = NAN;
  float brightReference_ = NAN;
  ThermoReading thermoReading_;
  LightReading lightReading_;
  DistanceReading distanceReading_;
  bool attempted_ = false;
  uint64_t lastAttempt_ = 0;
  bool lightAttempted_ = false;
  uint64_t lastLightAttempt_ = 0;
  bool distanceAttempted_ = false;
  uint64_t lastDistanceAttempt_ = 0;
};

}  // namespace contextcue
