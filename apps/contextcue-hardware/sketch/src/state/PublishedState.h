#pragma once

#include "../sensors/SensorReadings.h"

namespace contextcue {

// The completed readings and boot identity that RPC callbacks may observe.
// Only setup/loop touches SensorController; callbacks copy this snapshot.
struct PublishedState {
  ThermoReading thermo;
  LightReading light;
  DistanceReading distance;
  const char* driver = "none";
  char bootId[33] = "";
};

// Guarded by an internal mutex. Use the functions below; direct access is only
// for single-threaded host tests.
extern PublishedState publishedState;

// Publish completed readings. Never called across I2C work.
void publishState(const ThermoReading& thermo, const LightReading& light,
                  const DistanceReading& distance, const char* driver);

// Consistent copy of the published state for serialization outside the lock.
PublishedState snapshotState();

// Adopt `candidate` as the boot ID if none is assigned yet, then return a
// snapshot taken under the same lock. The caller validates the token format.
PublishedState adoptBootIdAndSnapshot(const char* candidate);

}  // namespace contextcue
