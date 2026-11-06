#include "BridgeRpc.h"

#include <Arduino_RouterBridge.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../Config.h"
#include "../Uptime.h"
#include "../state/PublishedState.h"

namespace contextcue {
namespace {

String checkedJson(const char* buffer, int length) {
  if (length < 0 || static_cast<size_t>(length) > kMaxJsonBytes) {
    return String("{\"v\":\"1.0\",\"err\":\"response_overflow\"}");
  }
  return String(buffer);
}

}  // namespace

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
  const PublishedState state = adoptBootIdAndSnapshot(candidate.c_str());
  char buffer[kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"fw\":\"%s\",\"boot\":\"%s\",\"ok\":%s,"
      "\"light\":%s,\"distance\":%s,\"driver\":\"%s\",\"core\":\"%s\",\"bridge\":\"%s\",\"modulino\":\"%s\",\"hs\":\"%s\",\"sht\":\"%s\"}",
      kSchemaVersion, kFirmwareVersion, state.bootId,
      state.thermo.available ? "true" : "false",
      state.light.available ? "true" : "false",
      state.distance.available ? "true" : "false", state.driver,
      kCoreVersion, kBridgeVersion,
      kModulinoVersion, kHsVersion, kShtVersion);
  return checkedJson(buffer, n);
}

String readState() {
  const PublishedState state = snapshotState();
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
  char buffer[kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"t\":%s,\"h\":%s,\"d\":\"%s\",\"err\":%s}",
      kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(now),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, temperature, humidity, state.driver, error);
  return checkedJson(buffer, n);
}

String readLightState() {
  const PublishedState state = snapshotState();
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
  char buffer[kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"lux\":%s,\"err\":%s}",
      kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(uptimeMs()),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, value, error);
  return checkedJson(buffer, n);
}

String readDistanceState() {
  const PublishedState state = snapshotState();
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
  char buffer[kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"mm\":%s,\"err\":%s}",
      kSchemaVersion, state.bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(uptimeMs()),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      quality, value, error);
  return checkedJson(buffer, n);
}

void beginBridgeRpc() {
  Bridge.begin();
  Bridge.provide_safe("cc_open", openSession);
  Bridge.provide_safe("cc_read", readState);
  Bridge.provide_safe("cc_read_light", readLightState);
  Bridge.provide_safe("cc_read_distance", readDistanceState);
}

}  // namespace contextcue
