// ContextCue first hardware milestone: Thermo -> Bridge -> Linux gateway.
// Sensor access stays in loop(); RPC callbacks read a protected published copy.
#include <Arduino_RouterBridge.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/kernel.h>

#include "Config.h"
#include "SensorController.h"

contextcue::SensorController sensors;

struct PublishedState {
  contextcue::ThermoReading thermo;
  contextcue::LightReading light;
  contextcue::DistanceReading distance;
  const char* driver = "none";
  char bootId[33] = "";
};

K_MUTEX_DEFINE(stateMutex);
PublishedState publishedState;

// Zephyr's 64-bit kernel uptime is safe to read from either thread.
uint64_t uptimeMs() {
  return static_cast<uint64_t>(k_uptime_get());
}

// Only loop/setup touches SensorController. Never hold this lock across I2C.
void publishState() {
  k_mutex_lock(&stateMutex, K_FOREVER);
  publishedState.thermo = sensors.reading();
  publishedState.light = sensors.lightReading();
  publishedState.distance = sensors.distanceReading();
  publishedState.driver = sensors.libraryName();
  k_mutex_unlock(&stateMutex);
}

String checkedJson(const char* buffer, int length) {
  if (length < 0 || static_cast<size_t>(length) > contextcue::kMaxJsonBytes) {
    return String("{\"v\":\"1.0\",\"err\":\"response_overflow\"}");
  }
  return String(buffer);
}

String openSession(String candidate) {
  if (candidate.length() != 32) {
    return String("{\"v\":\"1.0\",\"err\":\"invalid_boot_token\"}");
  }
  for (size_t i = 0; i < 32; ++i) {
    const char c = candidate[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
      return String("{\"v\":\"1.0\",\"err\":\"invalid_boot_token\"}");
    }
  }
  // A Linux-generated UUID names this firmware boot. It is assigned once and
  // retained across gateway reconnects; MCU restart clears it. It is an epoch
  // identity, not authentication and not a persisted user preference.
  k_mutex_lock(&stateMutex, K_FOREVER);
  if (publishedState.bootId[0] == '\0') {
    candidate.toCharArray(publishedState.bootId, sizeof(publishedState.bootId));
  }
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"fw\":\"%s\",\"boot\":\"%s\",\"ok\":%s,"
      "\"driver\":\"%s\",\"core\":\"%s\",\"bridge\":\"%s\",\"modulino\":\"%s\",\"hs\":\"%s\",\"sht\":\"%s\"}",
      contextcue::kSchemaVersion, contextcue::kFirmwareVersion, state.bootId,
      state.thermo.available ? "true" : "false", state.driver,
      contextcue::kCoreVersion, contextcue::kBridgeVersion,
      contextcue::kModulinoVersion, contextcue::kHsVersion, contextcue::kShtVersion);
  return checkedJson(buffer, n);
}

String readState() {
  k_mutex_lock(&stateMutex, K_FOREVER);
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  // Read time after the snapshot so its observation cannot be in the future.
  const uint64_t now = uptimeMs();
  const auto& sample = state.thermo;
  const char* quality = sample.quality;
  const char* reason = sample.error;
  char temperature[20] = "null";
  char humidity[20] = "null";
  char error[64] = "null";
  if (strcmp(quality, "valid") == 0) {
    if (reason == nullptr && sample.available &&
        isfinite(sample.temperature) && isfinite(sample.humidity)) {
      snprintf(temperature, sizeof(temperature), "%.2f", sample.temperature);
      snprintf(humidity, sizeof(humidity), "%.2f", sample.humidity);
    } else {
      quality = "invalid";
      if (reason == nullptr) reason = "thermo_read_failed";
    }
  } else if (reason == nullptr) {
    quality = "invalid";
    reason = "thermo_read_failed";
  }
  if (reason != nullptr) snprintf(error, sizeof(error), "\"%s\"", reason);
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"t\":%s,\"h\":%s,\"d\":\"%s\",\"err\":%s}",
      contextcue::kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(now),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, temperature, humidity, state.driver, error);
  return checkedJson(buffer, n);
}

String readLightState() {
  k_mutex_lock(&stateMutex, K_FOREVER);
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  const auto& sample = state.light;
  const char* quality = sample.quality;
  const char* reason = sample.error;
  char value[20] = "null";
  char error[64] = "null";
  if (strcmp(quality, "valid") == 0) {
    if (reason == nullptr && sample.available &&
        isfinite(sample.ambientLightLux)) {
      snprintf(value, sizeof(value), "%.2f", sample.ambientLightLux);
    } else {
      quality = "invalid";
      if (reason == nullptr) reason = "light_read_failed";
    }
  } else if (reason == nullptr) {
    quality = "invalid";
    reason = "light_read_failed";
  }
  if (reason != nullptr) snprintf(error, sizeof(error), "\"%s\"", reason);
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"lux\":%s,\"err\":%s}",
      contextcue::kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(uptimeMs()),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, value, error);
  return checkedJson(buffer, n);
}

String readDistanceState() {
  k_mutex_lock(&stateMutex, K_FOREVER);
  const PublishedState state = publishedState;
  k_mutex_unlock(&stateMutex);
  const auto& sample = state.distance;
  const char* quality = sample.quality;
  const char* reason = sample.error;
  char value[20] = "null";
  char error[64] = "null";
  if (strcmp(quality, "valid") == 0) {
    if (reason == nullptr && sample.available &&
        isfinite(sample.millimeters)) {
      snprintf(value, sizeof(value), "%.0f", sample.millimeters);
    } else {
      quality = "invalid";
      if (reason == nullptr) reason = "distance_read_failed";
    }
  } else if (reason == nullptr) {
    quality = "invalid";
    reason = "distance_read_failed";
  }
  if (reason != nullptr) snprintf(error, sizeof(error), "\"%s\"", reason);
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"mm\":%s,\"err\":%s}",
      contextcue::kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(uptimeMs()),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, value, error);
  return checkedJson(buffer, n);
}

void setup() {
  sensors.begin();
  sensors.readTemperatureHumidity(uptimeMs());
  publishState();
  Bridge.begin();
  Bridge.provide_safe("cc_open", openSession);
  Bridge.provide_safe("cc_read", readState);
  Bridge.provide_safe("cc_read_light", readLightState);
  Bridge.provide_safe("cc_read_distance", readDistanceState);
}

void loop() {
  const uint64_t now = uptimeMs();
  sensors.readThermoIfDue(now);
  publishState();
  sensors.readLightIfDue(now);
  sensors.readDistanceIfDue(now);
  // Keep callbacks safe even if the pinned RPC worker dispatches them early.
  // No Bridge.call(), nested RPC or serial printing from RPC callbacks.
  delay(1);
}
