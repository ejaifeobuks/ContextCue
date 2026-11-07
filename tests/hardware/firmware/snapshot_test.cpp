#include <atomic>
#include <cassert>
#include <iostream>
#include <limits>
#include <thread>

#include <zephyr/kernel.h>

// Exercise the actual callbacks, publication, controller and serializer. The
// sketch's src/*.cpp files are compiled alongside this file by test_firmware.py.
#include "../../../apps/contextcue-hardware/sketch/sketch.ino"

using namespace contextcue;

const String bootToken("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
const String otherToken("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");

void emit(const std::string& label, const String& payload) {
  std::cout << label << '\t' << payload << '\n';
}

void emitDistance(const std::string& label) {
  const auto& reading = sensors.distanceReading();
  std::cout << label << "\t{\"seq\":" << reading.sequence
            << ",\"at\":" << reading.observedUptimeMs
            << ",\"mm\":";
  if (std::isfinite(reading.millimeters)) {
    std::cout << reading.millimeters;
  } else {
    std::cout << "null";
  }
  std::cout << ",\"available\":" << (reading.available ? "true" : "false")
            << ",\"q\":\"" << reading.quality << "\",\"err\":";
  if (reading.error == nullptr) {
    std::cout << "null";
  } else {
    std::cout << "\"" << reading.error << "\"";
  }
  std::cout << "}\n";
}

void acquire(const std::string& label) {
  duringAcquisition = [&](const char* phase) {
    // Model the pinned RPC worker running during each blocking sensor call.
    // Joining here also verifies the writer holds no state lock across I2C.
    std::thread callback([&] {
      emit(label + "_during_" + phase, readState());
      emit(label + "_open_" + phase, openSession(otherToken));
    });
    callback.join();
  };
  testUptime += 1000;
  loop();
  duringAcquisition = {};
  emit(label + "_after", readState());
  emitDistance(label + "_distance_after");
}

// What physically reached the stub Pixels/Buzzer since boot.
void emitHardware(const std::string& label) {
  std::cout << label << "\t{\"leds\":[";
  for (int i = 0; i < 8; ++i) std::cout << (i ? "," : "") << testPixelsShown[i];
  std::cout << "],\"shows\":" << testPixelsShowCalls
            << ",\"tones\":" << testToneCalls << ",\"freq\":" << testToneFrequency
            << ",\"dur\":" << testToneDuration << ",\"notones\":" << testNoToneCalls << "}\n";
}

String out(const char* session, const char* id, const char* action,
           const char* pattern = "", int brightness = 0, int frequency = 0,
           int duration = 0, int ttl = 2000) {
  return executeOutput(String(session), String(id), String(action), String(pattern),
                       brightness, frequency, duration, ttl);
}

void tick(int64_t ms) {
  testUptime += ms;
  updateOutputs(uptimeMs());
}

void testOutputs() {
  emitHardware("out_boot_hw");  // setup() ran beginOutputs(): quiet boot
  emit("out_initial", readOutputs());
  emit("out_before_lease", out("s1", "c1", "pixels.set_pattern", "notice", 25));
  emit("out_lease", renewLease(String("s1")));
  emit("out_pattern", out("s1", "c1", "pixels.set_pattern", "notice", 25));
  emit("out_duplicate", out("s1", "c1", "pixels.set_pattern", "notice", 25));
  tick(10);
  emit("out_pattern_state", readOutputs());
  emitHardware("out_pattern_hw");

  // Every bound is enforced on the MCU, whatever Linux already checked.
  emit("rej_brightness", out("s1", "b1", "pixels.set_pattern", "notice", 101));
  emit("rej_pattern", out("s1", "b2", "pixels.set_pattern", "rainbow", 10));
  emit("rej_duration", out("s1", "b3", "buzzer.short_tone", "", 0, 880, 251));
  emit("rej_frequency", out("s1", "b4", "buzzer.short_tone", "", 0, 100, 100));
  emit("rej_ttl_zero", out("s1", "b5", "pixels.off", "", 0, 0, 0, 0));
  emit("rej_ttl_long", out("s1", "b6", "pixels.off", "", 0, 0, 0, 2001));
  emit("rej_action", out("s1", "b7", "gpio.write"));
  emit("rej_extra", out("s1", "b8", "pixels.off", "", 5));
  emit("rej_id", out("s1", "bad id", "pixels.off"));
  emit("rej_session", out("s2", "b9", "pixels.off"));
  emit("rej_long_session",
       out("ssssssssssssssssssssssssssssssssss", "b10", "pixels.off"));

  emit("out_tone", out("s1", "t1", "buzzer.short_tone", "", 0, 880, 200));
  tick(10);
  emit("out_tone_state", readOutputs());
  emitHardware("out_tone_hw");
  tick(300);
  emit("out_tone_done", readOutputs());

  // A command whose deadline passes before the loop runs is never applied.
  emit("out_short_ttl", out("s1", "e1", "pixels.set_pattern", "fault", 30, 0, 0, 5));
  tick(10);
  emit("out_expired_state", readOutputs());

  // Lease loss: no renewal for 3 s turns Pixels off and silences the Buzzer.
  emit("out_renew", renewLease(String("s1")));
  emit("out_pattern2", out("s1", "c2", "pixels.set_pattern", "ready", 40));
  tick(10);
  emitHardware("out_before_expiry_hw");
  tick(3001);
  emit("out_expired_lease", readOutputs());
  emitHardware("out_after_expiry_hw");
  emit("out_after_expiry_cmd", out("s1", "c3", "pixels.set_pattern", "notice", 25));
  emit("out_after_expiry_stop", out("s1", "c4", "outputs.stop"));
  tick(10);
  emit("out_after_expiry_stop_state", readOutputs());

  // A new session starts quiet and forgets the old session's command IDs.
  emit("out_new_session", renewLease(String("s2")));
  emit("out_new_session_reuse", out("s2", "c1", "pixels.set_pattern", "notice", 20));
  tick(10);
  emit("out_new_session_state", readOutputs());

  // Disconnect is detected by address probe; reconnect restores the pattern.
  pixelsReachable = false;
  tick(1000);
  emit("out_pixels_gone", readOutputs());
  emit("out_pixels_gone_cmd", out("s2", "d1", "pixels.set_pattern", "fault", 20));
  renewLease(String("s2"));
  pixelsReachable = true;
  tick(1000);
  emit("out_pixels_back", readOutputs());
  emitHardware("out_pixels_back_hw");
  emit("out_long_id", out("s2", "abcdefghijklmnopqrstuvwxyz012345", "outputs.stop"));
  tick(10);
  emit("out_long_id_state", readOutputs());
}

int main() {
  // Boot identity is assigned once even when handshake callbacks overlap.
  String responses[2];
  std::thread first([&] { responses[0] = openSession(bootToken); });
  std::thread second([&] { responses[1] = openSession(otherToken); });
  first.join();
  second.join();
  emit("open_first", responses[0]);
  emit("open_second", responses[1]);

  setup();
  emit("initial", readState());
  emit("initial_light", readLightState());
  emit("initial_distance_state", readDistanceState());
  emitDistance("initial_distance");
  emit("initial_open", openSession(bootToken));
  testTemperature = 26.5f;
  testHumidity = 50.0f;
  acquire("success");

  thermoReachable = false;
  acquire("disconnect");
  thermoReachable = true;
  acquire("recovery");
  testTemperature = NAN;
  acquire("bad_read");
  testTemperature = 200.0f;
  acquire("out_of_range");
  testTemperature = 24.0f;
  acquire("final_recovery");
  emit("light_after", readLightState());
  emit("distance_after", readDistanceState());

  distanceHasNewSample = false;
  acquire("distance_timeout");
  distanceHasNewSample = true;
  testDistanceMillimeters = 5001;
  acquire("distance_out_of_range");
  distanceReachable = false;
  acquire("distance_disconnect");
  distanceReachable = true;
  testDistanceMillimeters = 425;
  acquire("distance_reconnect");  // begin() runs; warm-up poll reads nothing
  acquire("distance_recovery");

  // Defense in depth: even an inconsistent published record stays valid JSON.
  const PublishedState good = publishedState;
  const float nonfinite[] = {NAN, INFINITY, -INFINITY};
  for (int metric = 0; metric < 2; ++metric) {
    for (int value = 0; value < 3; ++value) {
      publishedState = good;
      if (metric == 0) publishedState.thermo.temperature = nonfinite[value];
      else publishedState.thermo.humidity = nonfinite[value];
      emit("guard_" + std::to_string(metric) + "_" + std::to_string(value), readState());
    }
  }
  publishedState = good;

  testOutputs();

  // Cross the old millis() wrap boundary while callbacks and publication race.
  testUptime = (int64_t{1} << 32) - 2000;
  std::atomic<bool> done{false};
  std::thread reader([&] {
    int count = 0;
    while (!done.load() || count < 1000) {
      emit("stress", readState());
      ++count;
    }
  });
  for (int i = 0; i < 1000; ++i) {
    testTemperature = 20.0f + i % 10;
    testHumidity = 40.0f + i % 20;
    testUptime += 1000;
    loop();
  }
  done = true;
  reader.join();
  emit("after_wrap", readState());
  std::cout << "distance_begin_calls\t{\"count\":" << distanceBeginCalls << "}\n";
}
