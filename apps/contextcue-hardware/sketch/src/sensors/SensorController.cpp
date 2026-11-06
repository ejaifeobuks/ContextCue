#include "SensorController.h"

#include <math.h>

#include "../Config.h"

namespace contextcue {

void SensorController::begin() {
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

void SensorController::readThermoIfDue(uint64_t now) {
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

void SensorController::readTemperatureHumidity(uint64_t now) {
  readThermoIfDue(now);
}

void SensorController::readLightIfDue(uint64_t now) {
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

bool SensorController::setLightCalibration(float darkReference,
                                           float brightReference) {
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

void SensorController::readDistanceIfDue(uint64_t now) {
  if (distanceAttempted_ &&
      now - lastDistanceAttempt_ < kDistanceIntervalMs) {
    return;
  }
  distanceAttempted_ = true;
  lastDistanceAttempt_ = now;
  distanceReading_.sequence++;
  distanceReading_.observedUptimeMs = now;
  distanceReading_.millimeters = NAN;

  // Unlike Thermo/Light, Distance's begin() restarts the VL53L4CD ranging
  // state machine. Calling it every poll would reset an in-progress
  // measurement before it can ever report data ready, so only call it once
  // at init and again after a confirmed disconnect.
  if (!distanceInitialized_) {
    distanceInitialized_ = distance_.begin();
    if (!distanceInitialized_) {
      reportDistanceFailure(
          "unavailable",
          distanceSeenOnce_ ? "distance_disconnected" : "distance_not_found",
          true);
      return;
    }
    distanceSeenOnce_ = true;
    distanceConsecutiveTimeouts_ = 0;
  }
  if (!distance_.available()) {
    // A lone miss just means the current ranging cycle isn't done yet.
    // Only a prolonged silence gets treated as a reachability problem,
    // since re-probing with begin() on every miss would reset a healthy
    // in-progress cycle the same way polling it every loop once did.
    if (++distanceConsecutiveTimeouts_ >= kDistanceMaxConsecutiveTimeouts) {
      distanceConsecutiveTimeouts_ = 0;
      distanceInitialized_ = distance_.begin();
      if (!distanceInitialized_) {
        reportDistanceFailure("unavailable", "distance_disconnected", true);
        return;
      }
    }
    reportDistanceFailure("unavailable", "distance_timeout");
    return;
  }
  distanceConsecutiveTimeouts_ = 0;

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

const ThermoReading& SensorController::reading() const { return thermoReading_; }
const ThermoReading& SensorController::thermoReading() const { return thermoReading_; }
const LightReading& SensorController::lightReading() const { return lightReading_; }
const DistanceReading& SensorController::distanceReading() const {
  return distanceReading_;
}
bool SensorController::available() const { return thermoReading_.available; }
const char* SensorController::libraryName() const {
  return thermoInitialized_ ? "modulino" : "none";
}

bool SensorController::validateReading(float temperature, float humidity) const {
  return isfinite(temperature) && isfinite(humidity) &&
         temperature >= -40 && temperature <= 125 &&
         humidity >= 0 && humidity <= 100;
}

bool SensorController::validateLightReading(float lux) const {
  return isfinite(lux) && lux >= 0 && lux <= kLightMaximumLux;
}

float SensorController::normalizeLight(float lux) const {
  if (!lightCalibrationValid_) return NAN;
  const float normalized = (lux - darkReference_) /
                          (brightReference_ - darkReference_);
  return normalized < 0 ? 0 : (normalized > 1 ? 1 : normalized);
}

bool SensorController::validateDistanceReading(int millimeters) const {
  return millimeters >= 0 && millimeters <= kDistanceMaximumMm;
}

void SensorController::reportSensorFailure(const char* quality, const char* error) {
  thermoReading_.quality = quality;
  thermoReading_.error = error;
  thermoReading_.temperature = NAN;
  thermoReading_.humidity = NAN;
  thermoReading_.available = false;
  thermoInitialized_ = false;
}

void SensorController::reportLightFailure(const char* quality, const char* error) {
  lightReading_.quality = quality;
  lightReading_.error = error;
  lightReading_.ambientLightLux = NAN;
  lightReading_.normalizedLevel = NAN;
  lightReading_.available = false;
  lightInitialized_ = false;
}

void SensorController::reportDistanceFailure(const char* quality, const char* error,
                                             bool disconnected) {
  distanceReading_.quality = quality;
  distanceReading_.error = error;
  distanceReading_.millimeters = NAN;
  distanceReading_.available = false;
  distanceInitialized_ = !disconnected;
}

}  // namespace contextcue
