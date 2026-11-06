#include "PublishedState.h"

#include <stdio.h>
#include <zephyr/kernel.h>

K_MUTEX_DEFINE(stateMutex);

namespace contextcue {

PublishedState publishedState;

// Only loop/setup touches SensorController. Never hold this lock across I2C.
void publishState(const ThermoReading& thermo, const LightReading& light,
                  const DistanceReading& distance, const char* driver) {
  k_mutex_lock(&stateMutex, K_FOREVER);
  publishedState.thermo = thermo;
  publishedState.light = light;
  publishedState.distance = distance;
  publishedState.driver = driver;
  k_mutex_unlock(&stateMutex);
}

PublishedState snapshotState() {
  k_mutex_lock(&stateMutex, K_FOREVER);
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  return state;
}

PublishedState adoptBootIdAndSnapshot(const char* candidate) {
  // A Linux-generated UUID names this firmware boot. It is assigned once and
  // retained across gateway reconnects; MCU restart clears it. It is an epoch
  // identity, not authentication and not a persisted user preference.
  k_mutex_lock(&stateMutex, K_FOREVER);
  if (publishedState.bootId[0] == '\0') {
    // Same truncate-and-terminate copy as String::toCharArray().
    snprintf(publishedState.bootId, sizeof(publishedState.bootId), "%s", candidate);
  }
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  return state;
}

}  // namespace contextcue
