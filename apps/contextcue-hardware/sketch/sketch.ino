// ContextCue Modulino firmware: Thermo/Light/Distance -> Bridge -> Linux gateway.
// Sensor access stays in loop(); RPC callbacks read a protected published copy.
// Implementation lives in src/: sensors/ (acquisition), state/ (publication),
// rpc/ (Bridge handlers and serialization).
#include "src/Uptime.h"
#include "src/outputs/OutputController.h"
#include "src/rpc/BridgeRpc.h"
#include "src/sensors/SensorController.h"
#include "src/state/PublishedState.h"

contextcue::SensorController sensors;

// Only setup/loop touches SensorController. Never hold the state lock across I2C.
void publishSensors() {
  contextcue::publishState(sensors.reading(), sensors.lightReading(),
                           sensors.distanceReading(), sensors.libraryName());
}

void setup() {
  sensors.begin();
  contextcue::beginOutputs(contextcue::uptimeMs());  // quiet boot
  sensors.readTemperatureHumidity(contextcue::uptimeMs());
  publishSensors();
  contextcue::beginBridgeRpc();
}

void loop() {
  const uint64_t now = contextcue::uptimeMs();
  sensors.readThermoIfDue(now);
  publishSensors();
  sensors.readLightIfDue(now);
  sensors.readDistanceIfDue(now);
  contextcue::updateOutputs(contextcue::uptimeMs());
  // Keep callbacks safe even if the pinned RPC worker dispatches them early.
  // No Bridge.call(), nested RPC or serial printing from RPC callbacks.
  delay(1);
}
