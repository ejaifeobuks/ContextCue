#include <atomic>
#include <cassert>
#include <iostream>
#include <limits>
#include <thread>

// Exercise the actual callbacks, publication, controller and serializer.
#include "../../apps/contextcue_hardware/sketch/sketch.ino"

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
}
