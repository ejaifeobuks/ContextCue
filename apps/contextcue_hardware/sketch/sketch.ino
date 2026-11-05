// ContextCue first hardware milestone: Thermo -> Bridge -> Linux gateway.
// All callbacks and sensor access run in loop context via provide_safe().
#include <Arduino_RouterBridge.h>
#include <stdio.h>
#include <string.h>

#include "Config.h"
#include "SensorController.h"

contextcue::SensorController sensors;
char bootId[33] = "";

// Extend the 32-bit millis counter without guessing Linux wall-clock time.
// Called only in the main thread, at least once every millis wrap period.
uint64_t uptimeMs() {
  static uint32_t previous = 0;
  static uint64_t total = 0;
  const uint32_t current = millis();
  total += static_cast<uint32_t>(current - previous);
  previous = current;
  return total;
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
  if (bootId[0] == '\0') candidate.toCharArray(bootId, sizeof(bootId));
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"fw\":\"%s\",\"boot\":\"%s\",\"ok\":%s,"
      "\"driver\":\"%s\",\"core\":\"%s\",\"bridge\":\"%s\",\"modulino\":\"%s\",\"hs\":\"%s\",\"sht\":\"%s\"}",
      contextcue::kSchemaVersion, contextcue::kFirmwareVersion, bootId,
      sensors.available() ? "true" : "false", sensors.libraryName(),
      contextcue::kCoreVersion, contextcue::kBridgeVersion,
      contextcue::kModulinoVersion, contextcue::kHsVersion, contextcue::kShtVersion);
  return checkedJson(buffer, n);
}

String readState() {
  const uint64_t now = uptimeMs();
  const auto& sample = sensors.reading();
  char temperature[20] = "null";
  char humidity[20] = "null";
  char error[64] = "null";
  if (sample.error == nullptr) {
    snprintf(temperature, sizeof(temperature), "%.2f", sample.temperature);
    snprintf(humidity, sizeof(humidity), "%.2f", sample.humidity);
  } else {
    snprintf(error, sizeof(error), "\"%s\"", sample.error);
  }
  char buffer[contextcue::kMaxJsonBytes + 1];
  const int n = snprintf(
      buffer, sizeof(buffer),
      "{\"v\":\"%s\",\"boot\":\"%s\",\"seq\":%llu,\"up\":%llu,\"at\":%llu,"
      "\"q\":\"%s\",\"t\":%s,\"h\":%s,\"d\":\"%s\",\"err\":%s}",
      contextcue::kSchemaVersion, bootId,
      static_cast<unsigned long long>(sample.sequence),
      static_cast<unsigned long long>(now),
      static_cast<unsigned long long>(sample.observedUptimeMs),
      sample.quality, temperature, humidity, sensors.libraryName(), error);
  return checkedJson(buffer, n);
}

void setup() {
  sensors.begin();
  sensors.readTemperatureHumidity(uptimeMs());
  Bridge.begin();
  Bridge.provide_safe("cc_open", openSession);
  Bridge.provide_safe("cc_read", readState);
}

void loop() {
  const uint64_t now = uptimeMs();
  sensors.readThermoIfDue(now);
  sensors.readLightIfDue(now);
  // Returning promptly lets the Arduino core dispatch provide_safe handlers.
  // No Bridge.call(), nested RPC or serial printing from RPC callbacks.
  delay(1);
}
