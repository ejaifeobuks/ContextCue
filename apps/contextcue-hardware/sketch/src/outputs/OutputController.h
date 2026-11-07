#pragma once

#include <stdint.h>

#include "../Config.h"

namespace contextcue {

// Bounded Pixels/Buzzer outputs (baseline HW-03, HW-07).
//
// RPC callbacks only validate and queue (submitOutputCommand, renewOutputLease);
// the main loop performs all I2C writes in updateOutputs(). The MCU enforces
// the allowlist, bounds, TTL, session, duplicate IDs and the output lease even
// when Linux has already validated a command.

// Published copy for cc_read_outputs. Strings point to static literals or to
// the fixed buffers inside this struct.
struct OutputStatus {
  bool pixelsAvailable = false;
  bool buzzerAvailable = false;
  const char* pattern = "off";
  int brightnessPercent = 0;
  bool toneActive = false;
  bool leaseActive = false;
  char lastId[kMaxIdLength + 1] = "";
  const char* lastOutcome = nullptr;  // completed, failed, expired
  const char* lastReason = nullptr;
};

// Validate a command and queue it for the loop. Returns nullptr when accepted,
// otherwise a short rejection reason. Safe to call from any thread.
const char* submitOutputCommand(const char* session, const char* commandId,
                                const char* action, const char* pattern,
                                int brightnessPercent, int frequencyHz,
                                int durationMs, int ttlMs, uint64_t now);

// Start or renew the lease for `session`. A different session starts quiet:
// outputs go off and earlier command IDs and queued commands are dropped.
// Returns nullptr on success, otherwise a reason.
const char* renewOutputLease(const char* session, uint64_t now);

OutputStatus snapshotOutputs();

// Main loop only. begin() puts outputs into the quiet state.
void beginOutputs(uint64_t now);
void updateOutputs(uint64_t now);

}  // namespace contextcue
