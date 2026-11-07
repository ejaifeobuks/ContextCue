#pragma once

#include <cstdint>
#include <functional>

inline bool thermoReachable = true;
inline float testTemperature = 25.75f;
inline float testHumidity = 65.52f;
inline bool distanceReachable = true;
inline bool distanceHasNewSample = true;
inline int testDistanceMillimeters = 430;
inline int distanceBeginCalls = 0;
inline std::function<void(const char*)> duringAcquisition;

inline bool pixelsReachable = true;
inline bool buzzerReachable = true;

// Qwiic bus: address probes for Distance, Pixels (0x36) and Buzzer (0x1E).
struct TestWire {
  int address = -1;
  void beginTransmission(int value) { address = value; }
  int endTransmission() {
    if (address == 0x29 && !distanceReachable) return 2;
    if (address == 0x36 && !pixelsReachable) return 2;
    if (address == 0x1E && !buzzerReachable) return 2;
    return 0;
  }
};
inline TestWire Wire1;

// Output modules record what reached the "hardware" in globals, because the
// firmware's instances are private to OutputController.cpp.
inline uint32_t testPixelsShown[8] = {};
inline int testPixelsShowCalls = 0;
inline int testToneFrequency = 0;
inline int testToneDuration = 0;
inline int testToneCalls = 0;
inline int testNoToneCalls = 0;

struct ModulinoPixels {
  uint32_t pending[8] = {};
  bool begin() { return pixelsReachable; }
  void set(int index, uint8_t r, uint8_t g, uint8_t b, uint8_t brightness) {
    if (index >= 0 && index < 8) {
      pending[index] = (uint32_t(r) << 24) | (uint32_t(g) << 16) | (uint32_t(b) << 8) | brightness;
    }
  }
  void clear() { for (auto& led : pending) led = 0; }
  void show() {
    for (int i = 0; i < 8; ++i) testPixelsShown[i] = pending[i];
    ++testPixelsShowCalls;
  }
};

struct ModulinoBuzzer {
  bool begin() { return buzzerReachable; }
  void tone(size_t frequency, size_t duration) {
    testToneFrequency = int(frequency);
    testToneDuration = int(duration);
    ++testToneCalls;
  }
  void noTone() { ++testNoToneCalls; }
};

struct TestModulino { void begin(TestWire&) {} };
inline TestModulino Modulino;

struct ModulinoThermo {
  bool begin() {
    if (duringAcquisition) duringAcquisition("begin");
    return thermoReachable;
  }
  float getTemperature() {
    if (duringAcquisition) duringAcquisition("temperature");
    return testTemperature;
  }
  float getHumidity() {
    if (duringAcquisition) duringAcquisition("humidity");
    return testHumidity;
  }
};

struct ModulinoLight {
  bool begin() { return false; }
  bool update() { return false; }
  int getAL() { return 0; }
};

struct ModulinoDistance {
  bool begin() {
    ++distanceBeginCalls;
    return distanceReachable;
  }
  bool available() { return distanceHasNewSample; }
  int get() { return testDistanceMillimeters; }
};
