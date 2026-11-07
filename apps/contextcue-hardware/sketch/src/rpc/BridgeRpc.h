#pragma once

#include <Arduino.h>

namespace contextcue {

// ContextCue Bridge RPC handlers (cc_open, cc_read, cc_read_light,
// cc_read_distance, cc_out, cc_lease, cc_read_outputs). Each copies the published state and serializes outside
// the lock. No Bridge.call(), nested RPC or serial printing from callbacks.
String openSession(String candidate);
String readState();
String readLightState();
String readDistanceState();
// Outputs: cc_out queues one allowlisted command (accepted/rejected only);
// cc_lease renews the output lease; cc_read_outputs reports state and the last
// command outcome (completed/failed/expired).
String executeOutput(String session, String commandId, String action,
                     String pattern, int brightnessPercent, int frequencyHz,
                     int durationMs, int ttlMs);
String renewLease(String session);
String readOutputs();

// Start the Bridge and register the handlers above.
void beginBridgeRpc();

}  // namespace contextcue
