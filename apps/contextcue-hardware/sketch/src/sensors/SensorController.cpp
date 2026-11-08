#include "SensorController.h"

#include <math.h>

#include "../Config.h"
#include "../Uptime.h"
#include "DivideByZeroTrap.h"
#include "QwiicBus.h"

namespace contextcue {
namespace {

// Address-only I2C probe on the Qwiic bus; the same check
// ModulinoDistance::begin() starts with, without its allocation/reset.
bool distanceResponds() {
  qwiicBus().beginTransmission(kDistanceI2cAddress);
  return qwiicBus().endTransmission() == 0;
}

}  // namespace

void SensorController::begin() {
  // Every Modulino driver talks through the guarded Qwiic bus.
  Modulino.begin(qwiicBus());
  thermoInitialized_ = thermo_.begin();
  lightInitialized_ = light_.begin();
  distanceInitialized_ = beginDistance(uptimeMs());
  distanceSeenOnce_ = distanceInitialized_;
  distanceWarmingUp_ = distanceInitialized_;
  thermoReading_.available = thermoInitialized_;
  thermoReading_.quality = "unavailable";
  thermoReading_.error = thermoInitialized_ ? "not_sampled" : "thermo_not_found";
  lightReading_.available = false;
  lightReading_.quality = "unavailable";
  lightReading_.error = lightInitialized_ ? "not_sampled" : "light_not_found";
  distanceReading_.available = false;
  distanceReading_.quality = "unavailable";
  distanceReading_.error =
      distanceInitialized_ ? "not_sampled"
                           : (distanceBusFault_ ? "distance_bus_error" : "distance_not_found");
  busStalled(uptimeMs());
}

// The Distance drivers retry a failed register access forever; a guarded
// section turns that into a reported fault (see BusGuard.h).
bool SensorController::beginDistance(uint64_t now) {
  GuardedSection guard;
  const bool started = distance_.begin();
  distanceBusFault_ = guard.finish();
  if (distanceBusFault_) {
    // begin() returned with garbage from skipped transfers. Retry later, but
    // back off: each begin() allocates driver objects that are never freed.
    distanceBeginRetryAt_ = now + distanceBeginBackoffMs_;
    distanceBeginBackoffMs_ = distanceBeginBackoffMs_ * 2 > kDistanceBeginMaxBackoffMs
                                  ? kDistanceBeginMaxBackoffMs
                                  : distanceBeginBackoffMs_ * 2;
    return false;
  }
  if (started) distanceBeginBackoffMs_ = kDistanceFaultBackoffMs;
  return started;
}

// True while acquisition is paused because the bus is being held. Entering
// the pause reports every sensor once, so Linux sees why samples stopped.
bool SensorController::busStalled(uint64_t now) {
  BusGuard& guard = qwiicBus().guard();
  if (guard.consumeSlow()) {
    guard.pauseUntil(now + kBusBackoffMs);
    ++busStalls_;
    reportSensorFailure("unavailable", "i2c_bus_stalled");
    reportLightFailure("unavailable", "i2c_bus_stalled");
    distanceReading_.quality = "unavailable";
    distanceReading_.error = "i2c_bus_stalled";
    distanceReading_.millimeters = NAN;
    distanceReading_.available = false;  // driver kept: not a disconnect
  }
  return guard.paused(now);
}

void SensorController::readThermoIfDue(uint64_t now) {
  if (busStalled(now)) return;
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
  if (busStalled(now)) return;
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
  if (busStalled(now)) return;
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
  bool updated;
  {
    // The Light library divides by the raw ALS count, which is 0 in darkness.
    DivideByZeroTrapOff noTrap;
    updated = light_.update();
  }
  if (!updated) {
    reportLightFailure("invalid", "light_read_failed");
    return;
  }

  const float lux = static_cast<float>(light_.getAL());
  if (!validateLightReading(lux)) {
    reportLightFailure("invalid", "light_out_of_range");
    return;
  }
  if (busStalled(now)) return;
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
  if (busStalled(now)) return;
  if (distanceAttempted_ &&
      now - lastDistanceAttempt_ < kDistanceIntervalMs) {
    return;
  }
  // After a bus fault, leave the sensor alone for a while.
  if (now < distanceRetryAt_) return;
  if (!distanceInitialized_ && now < distanceBeginRetryAt_) return;
  distanceAttempted_ = true;
  lastDistanceAttempt_ = now;
  distanceReading_.sequence++;
  distanceReading_.observedUptimeMs = now;
  distanceReading_.millimeters = NAN;

  // Unlike Thermo/Light, Distance's begin() allocates new driver objects
  // (never freed) and restarts the ToF ranging cycle. Call it only while the
  // sensor is not initialized: at boot or after a confirmed disconnect.
  if (!distanceInitialized_) {
    distanceInitialized_ = beginDistance(now);
    if (!distanceInitialized_) {
      reportDistanceFailure(
          "unavailable",
          distanceBusFault_ ? "distance_bus_error"
                            : (distanceSeenOnce_ ? "distance_disconnected" : "distance_not_found"),
          true);
      return;
    }
    distanceSeenOnce_ = true;
    distanceWarmingUp_ = true;
  } else if (!distanceResponds()) {
    // A held bus is not a disconnect: pause instead of re-running begin().
    if (busStalled(now)) return;
    // available() keeps returning the last result when the bus read fails,
    // so a bare address probe is the only reliable disconnect signal. It
    // neither allocates nor disturbs an in-progress measurement.
    reportDistanceFailure("unavailable", "distance_disconnected", true);
    return;
  }
  if (distanceWarmingUp_) {
    // begin() does not clear the library's last stored result, so the first
    // available() after it can return a pre-disconnect value (or a zeroed
    // "valid" 0 mm at boot). Skip one poll: ranging runs every 20 ms, so the
    // next poll (100 ms later) always replaces that result with a fresh one.
    distanceWarmingUp_ = false;
    reportDistanceFailure("unavailable", "distance_timeout");
    return;
  }
  bool fresh;
  {
    GuardedSection guard;
    fresh = distance_.available();
    if (guard.finish()) {
      // The driver gave up mid-read; whatever it returned is not a sample.
      // Keep the driver (begin() leaks) and try again after a pause.
      ++distanceBusFaults_;
      distanceRetryAt_ = now + kDistanceFaultBackoffMs;
      reportDistanceFailure("unavailable", "distance_bus_error");
      return;
    }
  }
  if (busStalled(now)) return;
  if (!fresh) {
    // Present but no in-range result: report it without re-initializing.
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
