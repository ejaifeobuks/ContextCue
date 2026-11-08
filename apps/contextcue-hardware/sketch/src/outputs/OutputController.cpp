#include "OutputController.h"

#include <Arduino_Modulino.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/kernel.h>

#include "../sensors/QwiicBus.h"

K_MUTEX_DEFINE(outputMutex);

namespace contextcue {
namespace {

enum class Action : uint8_t { PixelsSetPattern, PixelsOff, BuzzerShortTone, BuzzerStop, OutputsStop };
enum class Pattern : uint8_t { Off, Ready, Notice, Fault };

struct Command {
  char id[kMaxIdLength + 1];
  Action action;
  Pattern pattern;
  int brightnessPercent;
  int frequencyHz;
  int durationMs;
  uint64_t deadlineMs;
};

// Guarded by outputMutex: written by RPC callbacks and the loop.
struct Shared {
  char session[kMaxIdLength + 1] = "";
  uint64_t leaseExpiresMs = 0;
  bool leaseActive = false;
  bool quietRequested = false;  // loop applies the safe state
  char recentIds[kRecentCommandIds][kMaxIdLength + 1] = {};
  size_t recentNext = 0;
  Command queue[kOutputQueueLength] = {};
  size_t queueHead = 0;
  size_t queueCount = 0;
  OutputStatus status;
};
Shared shared;

// Loop-only hardware state; never touched by callbacks.
ModulinoPixels pixels;
ModulinoBuzzer buzzer;
bool pixelsBegun = false;
bool buzzerBegun = false;
bool probed = false;
uint64_t lastProbeMs = 0;
uint64_t toneEndsAtMs = 0;
Pattern desiredPattern = Pattern::Off;
int desiredBrightness = 0;

const char* patternName(Pattern pattern) {
  switch (pattern) {
    case Pattern::Ready: return "ready";
    case Pattern::Notice: return "notice";
    case Pattern::Fault: return "fault";
    default: return "off";
  }
}

bool validId(const char* text) {
  if (text == nullptr) return false;
  const size_t length = strlen(text);
  if (length == 0 || length > kMaxIdLength) return false;
  for (size_t i = 0; i < length; ++i) {
    const char c = text[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                    (c >= '0' && c <= '9') || c == '-' || c == '_';
    if (!ok) return false;
  }
  return true;
}

bool parseAction(const char* text, Action* action) {
  static const struct { const char* name; Action value; } kActions[] = {
      {"pixels.set_pattern", Action::PixelsSetPattern},
      {"pixels.off", Action::PixelsOff},
      {"buzzer.short_tone", Action::BuzzerShortTone},
      {"buzzer.stop", Action::BuzzerStop},
      {"outputs.stop", Action::OutputsStop},
  };
  for (const auto& entry : kActions) {
    if (strcmp(text, entry.name) == 0) {
      *action = entry.value;
      return true;
    }
  }
  return false;
}

bool parsePattern(const char* text, Pattern* pattern) {
  static const struct { const char* name; Pattern value; } kPatterns[] = {
      {"off", Pattern::Off}, {"ready", Pattern::Ready},
      {"notice", Pattern::Notice}, {"fault", Pattern::Fault},
  };
  for (const auto& entry : kPatterns) {
    if (strcmp(text, entry.name) == 0) {
      *pattern = entry.value;
      return true;
    }
  }
  return false;
}

bool isStop(Action action) {
  return action == Action::PixelsOff || action == Action::BuzzerStop ||
         action == Action::OutputsStop;
}

// Caller holds outputMutex.
void startSession(const char* session) {
  snprintf(shared.session, sizeof(shared.session), "%s", session);
  memset(shared.recentIds, 0, sizeof(shared.recentIds));
  shared.recentNext = 0;
  shared.queueHead = 0;
  shared.queueCount = 0;  // never carry another session's commands forward
  shared.quietRequested = true;
}

// Same bus object as the Modulino drivers, so slow probes count as stalls.
bool reachable(uint8_t address) {
  qwiicBus().beginTransmission(address);
  return qwiicBus().endTransmission() == 0;
}

void writePixels(Pattern pattern, int brightnessPercent) {
  pixels.clear();
  switch (pattern) {
    case Pattern::Ready:
      pixels.set(0, 0, 255, 0, brightnessPercent);
      break;
    case Pattern::Notice:
      for (int i = 0; i < 8; ++i) pixels.set(i, 255, 140, 0, brightnessPercent);
      break;
    case Pattern::Fault:
      for (int i = 0; i < 8; i += 2) pixels.set(i, 255, 0, 0, brightnessPercent);
      break;
    case Pattern::Off:
      break;
  }
  pixels.show();
}

// Loop only. Returns false when a required module is not reachable.
bool silence(bool pixelsOk, bool buzzerOk) {
  bool done = true;
  if (buzzerOk) buzzer.noTone(); else done = false;
  toneEndsAtMs = 0;
  desiredPattern = Pattern::Off;
  desiredBrightness = 0;
  if (pixelsOk) writePixels(Pattern::Off, 0); else done = false;
  return done;
}

void recordResult(const char* id, const char* outcome, const char* reason) {
  k_mutex_lock(&outputMutex, K_FOREVER);
  snprintf(shared.status.lastId, sizeof(shared.status.lastId), "%s", id);
  shared.status.lastOutcome = outcome;
  shared.status.lastReason = reason;
  k_mutex_unlock(&outputMutex);
}

// Loop only: run one dequeued command against the hardware.
void apply(const Command& command, uint64_t now) {
  if (now > command.deadlineMs) {
    recordResult(command.id, "expired", "ttl_elapsed");
    return;
  }
  const bool pixelsOk = pixelsBegun && reachable(kPixelsI2cAddress);
  const bool buzzerOk = buzzerBegun && reachable(kBuzzerI2cAddress);
  switch (command.action) {
    case Action::PixelsSetPattern:
      if (!pixelsOk) break;
      desiredPattern = command.pattern;
      desiredBrightness = command.brightnessPercent;
      writePixels(desiredPattern, desiredBrightness);
      recordResult(command.id, "completed", nullptr);
      return;
    case Action::PixelsOff:
      desiredPattern = Pattern::Off;
      desiredBrightness = 0;
      if (!pixelsOk) break;
      writePixels(Pattern::Off, 0);
      recordResult(command.id, "completed", nullptr);
      return;
    case Action::BuzzerShortTone:
      if (!buzzerOk) {
        recordResult(command.id, "failed", "buzzer_unavailable");
        return;
      }
      // The Buzzer module times the tone itself; no looping is possible.
      buzzer.tone(command.frequencyHz, command.durationMs);
      toneEndsAtMs = now + command.durationMs;
      recordResult(command.id, "completed", nullptr);
      return;
    case Action::BuzzerStop:
      toneEndsAtMs = 0;
      if (!buzzerOk) {
        recordResult(command.id, "failed", "buzzer_unavailable");
        return;
      }
      buzzer.noTone();
      recordResult(command.id, "completed", nullptr);
      return;
    case Action::OutputsStop:
      recordResult(command.id, silence(pixelsOk, buzzerOk) ? "completed" : "failed",
                   (pixelsOk && buzzerOk) ? nullptr : "output_unavailable");
      return;
  }
  recordResult(command.id, "failed", "pixels_unavailable");
}

}  // namespace

const char* submitOutputCommand(const char* session, const char* commandId,
                                const char* action, const char* pattern,
                                int brightnessPercent, int frequencyHz,
                                int durationMs, int ttlMs, uint64_t now) {
  if (!validId(session)) return "invalid_session";
  if (!validId(commandId)) return "invalid_command_id";
  Command command = {};
  if (action == nullptr || !parseAction(action, &command.action)) return "unknown_action";
  if (ttlMs <= 0 || ttlMs > kMaxCommandTtlMs) return "invalid_ttl";
  // Unused parameters must be zero/empty so a malformed caller is visible.
  const bool usesPattern = command.action == Action::PixelsSetPattern;
  const bool usesTone = command.action == Action::BuzzerShortTone;
  if (usesPattern) {
    if (pattern == nullptr || !parsePattern(pattern, &command.pattern)) return "invalid_pattern";
    if (brightnessPercent < 0 || brightnessPercent > kMaxBrightnessPercent) return "invalid_brightness";
  } else if ((pattern != nullptr && pattern[0] != '\0') || brightnessPercent != 0) {
    return "unexpected_parameter";
  }
  if (usesTone) {
    if (frequencyHz < kMinToneFrequencyHz || frequencyHz > kMaxToneFrequencyHz) return "invalid_frequency";
    if (durationMs <= 0 || durationMs > kMaxToneDurationMs) return "invalid_duration";
  } else if (frequencyHz != 0 || durationMs != 0) {
    return "unexpected_parameter";
  }
  snprintf(command.id, sizeof(command.id), "%s", commandId);
  command.brightnessPercent = brightnessPercent;
  command.frequencyHz = frequencyHz;
  command.durationMs = durationMs;
  command.deadlineMs = now + static_cast<uint64_t>(ttlMs);

  const char* reason = nullptr;
  k_mutex_lock(&outputMutex, K_FOREVER);
  const bool leased = shared.leaseActive && now < shared.leaseExpiresMs;
  if (shared.session[0] == '\0') {
    reason = "no_lease";  // no session has ever been leased since boot
  } else if (strcmp(shared.session, session) != 0) {
    reason = "session_mismatch";
  } else if (!leased && !isStop(command.action)) {
    reason = "no_lease";  // stops stay allowed: they can only make outputs safer
  } else if (usesPattern && !shared.status.pixelsAvailable) {
    reason = "pixels_unavailable";
  } else if (usesTone && !shared.status.buzzerAvailable) {
    reason = "buzzer_unavailable";
  } else {
    for (const auto& recent : shared.recentIds) {
      if (strcmp(recent, commandId) == 0) {
        reason = "duplicate_command";
        break;
      }
    }
    if (reason == nullptr && shared.queueCount == kOutputQueueLength) reason = "busy";
  }
  if (reason == nullptr) {
    snprintf(shared.recentIds[shared.recentNext], kMaxIdLength + 1, "%s", commandId);
    shared.recentNext = (shared.recentNext + 1) % kRecentCommandIds;
    shared.queue[(shared.queueHead + shared.queueCount) % kOutputQueueLength] = command;
    ++shared.queueCount;
  }
  k_mutex_unlock(&outputMutex);
  return reason;
}

const char* renewOutputLease(const char* session, uint64_t now) {
  if (!validId(session)) return "invalid_session";
  k_mutex_lock(&outputMutex, K_FOREVER);
  if (strcmp(shared.session, session) != 0) startSession(session);
  shared.leaseActive = true;
  shared.leaseExpiresMs = now + kOutputLeaseMs;
  shared.status.leaseActive = true;
  k_mutex_unlock(&outputMutex);
  return nullptr;
}

OutputStatus snapshotOutputs() {
  k_mutex_lock(&outputMutex, K_FOREVER);
  const OutputStatus status = shared.status;
  k_mutex_unlock(&outputMutex);
  return status;
}

void beginOutputs(uint64_t now) {
  // Quiet boot: whatever the modules were doing before a reset stops here.
  pixelsBegun = pixels.begin();
  buzzerBegun = buzzer.begin();
  silence(pixelsBegun, buzzerBegun);
  lastProbeMs = now;
  probed = true;
  k_mutex_lock(&outputMutex, K_FOREVER);
  shared.status.pixelsAvailable = pixelsBegun;
  shared.status.buzzerAvailable = buzzerBegun;
  k_mutex_unlock(&outputMutex);
}

void updateOutputs(uint64_t now) {
  // While the Qwiic bus is held, every write would stall loop() for the
  // driver's 500 ms timeout. Leave state as is: queued commands keep their TTL
  // and the lease watchdog runs once the bus is released.
  if (qwiicBus().guard().paused(now)) return;
  // Availability: address probes, since the library ignores write errors.
  if (!probed || now - lastProbeMs >= kOutputProbeIntervalMs) {
    probed = true;
    lastProbeMs = now;
    // begin() only discovers the address; once found it keeps returning true.
    if (!pixelsBegun) pixelsBegun = pixels.begin();
    if (!buzzerBegun) buzzerBegun = buzzer.begin();
    const bool pixelsNow = pixelsBegun && reachable(kPixelsI2cAddress);
    const bool buzzerNow = buzzerBegun && reachable(kBuzzerI2cAddress);
    k_mutex_lock(&outputMutex, K_FOREVER);
    const bool pixelsReturned = pixelsNow && !shared.status.pixelsAvailable;
    shared.status.pixelsAvailable = pixelsNow;
    shared.status.buzzerAvailable = buzzerNow;
    k_mutex_unlock(&outputMutex);
    // A reconnected strip shows only the current pattern (off without a lease).
    if (pixelsReturned) writePixels(desiredPattern, desiredBrightness);
  }

  // Lease watchdog and session changes.
  bool quiet = false;
  k_mutex_lock(&outputMutex, K_FOREVER);
  if (shared.leaseActive && now >= shared.leaseExpiresMs) {
    shared.leaseActive = false;
    shared.status.leaseActive = false;
    shared.queueHead = 0;
    shared.queueCount = 0;
    quiet = true;
  }
  if (shared.quietRequested) {
    shared.quietRequested = false;
    quiet = true;
  }
  const bool pixelsOk = shared.status.pixelsAvailable;
  const bool buzzerOk = shared.status.buzzerAvailable;
  k_mutex_unlock(&outputMutex);
  if (quiet) silence(pixelsOk, buzzerOk);

  // Drain queued commands outside the lock; I2C never runs under it.
  while (true) {
    Command command;
    k_mutex_lock(&outputMutex, K_FOREVER);
    const bool have = shared.queueCount > 0;
    if (have) {
      command = shared.queue[shared.queueHead];
      shared.queueHead = (shared.queueHead + 1) % kOutputQueueLength;
      --shared.queueCount;
    }
    k_mutex_unlock(&outputMutex);
    if (!have) break;
    apply(command, now);
  }

  k_mutex_lock(&outputMutex, K_FOREVER);
  shared.status.pattern = patternName(desiredPattern);
  shared.status.brightnessPercent = desiredBrightness;
  shared.status.toneActive = toneEndsAtMs != 0 && now < toneEndsAtMs;
  k_mutex_unlock(&outputMutex);
}

}  // namespace contextcue
