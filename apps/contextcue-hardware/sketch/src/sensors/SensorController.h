#pragma once

#include <Arduino_Modulino.h>
#include <stdint.h>

#include "SensorReadings.h"

namespace contextcue {

// Hardware acquisition only. Comfort policy and calendar time belong on Linux.
// Arduino_Modulino owns Thermo detection and the underlying sensor drivers.
class SensorController {
 public:
  void begin();

  void readThermoIfDue(uint64_t now);
  // Compatibility wrapper for the first Thermo-only firmware milestone.
  void readTemperatureHumidity(uint64_t now);
  void readLightIfDue(uint64_t now);
  void readDistanceIfDue(uint64_t now);

  bool setLightCalibration(float darkReference, float brightReference);

  const ThermoReading& reading() const;
  const ThermoReading& thermoReading() const;
  const LightReading& lightReading() const;
  const DistanceReading& distanceReading() const;
  bool available() const;
  const char* libraryName() const;

  bool validateReading(float temperature, float humidity) const;
  bool validateLightReading(float lux) const;
  float normalizeLight(float lux) const;
  bool validateDistanceReading(int millimeters) const;

  void reportSensorFailure(const char* quality, const char* error);
  void reportLightFailure(const char* quality, const char* error);
  void reportDistanceFailure(const char* quality, const char* error,
                             bool disconnected = false);

 private:
  ModulinoThermo thermo_;
  ModulinoLight light_;
  ModulinoDistance distance_;
  bool thermoInitialized_ = false;
  bool lightInitialized_ = false;
  bool distanceInitialized_ = false;
  bool distanceSeenOnce_ = false;
  uint32_t distanceConsecutiveTimeouts_ = 0;
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
