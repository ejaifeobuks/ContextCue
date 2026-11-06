#pragma once

#include <Arduino.h>

namespace contextcue {

// ContextCue Bridge RPC handlers (cc_open, cc_read, cc_read_light,
// cc_read_distance). Each copies the published state and serializes outside
// the lock. No Bridge.call(), nested RPC or serial printing from callbacks.
String openSession(String candidate);
String readState();
String readLightState();
String readDistanceState();

// Start the Bridge and register the handlers above.
void beginBridgeRpc();

}  // namespace contextcue
