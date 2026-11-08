#pragma once

#include <cstdint>
#include <functional>

#include <Wire.h>

inline bool thermoReachable = true;
inline float testTemperature = 25.75f;
inline float testHumidity = 65.52f;
inline bool distanceHasNewSample = true;
inline int testDistanceMillimeters = 430;
inline int distanceBeginCalls = 0;
inline std::function<void(const char*)> duringAcquisition;

// The bus the firmware hands to Modulino.begin(); drivers use it as on the board.
inline arduino::ZephyrI2C* testBus = nullptr;
// Times the stub driver hit its safety cap: on the board it would hang forever.
inline int testDriverHangs = 0;

// Mirrors STM32duino VL53L4CD 1.0.5 platform.cpp: the register write is
// retried with `while (status != 0)` and no limit, then the data is read.
inline bool distanceRegisterRead() {
  long attempts = 0;
  uint8_t status;
  do {
    testBus->beginTransmission(0x29);
    const uint8_t reg[2] = {0x00, 0x31};
    testBus->write(reg, 2);
    status = testBus->endTransmission(false);
    if (++attempts > 100000) {
      ++testDriverHangs;
      return false;
    }
  } while (status != 0);
  return testBus->requestFrom(0x29, 1) == 1;
}

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

struct TestModulino {
  void begin(arduino::ZephyrI2C& wire) {
    testBus = &wire;
    wire.begin();
    wire.setClock(100000);
  }
};
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

namespace contextcue { int divideByZeroTrapOffDepth(); }

inline bool lightReachable = false;  // most tests run without a Light module
inline int testLightRaw = 120;       // raw ALS count; 0 in darkness
// Dark readings taken with the divide-by-zero trap on: each one halts the
// real MCU (UsageFault in Arduino_LTR381RGB getALS, `ir / rawlux`).
inline int testDivideByZeroFaults = 0;

struct ModulinoLight {
  int lux = 0;
  bool begin() { return lightReachable; }
  bool update() {
    if (!lightReachable) return false;
    if (testLightRaw == 0 && contextcue::divideByZeroTrapOffDepth() == 0) {
      ++testDivideByZeroFaults;
      return false;
    }
    lux = testLightRaw == 0 ? 0 : testLightRaw / 2;
    return true;
  }
  int getAL() { return lux; }
};

struct ModulinoDistance {
  bool begin() {
    ++distanceBeginCalls;
    // Like the library: address probe, then InitSensor's register accesses.
    testBus->beginTransmission(0x29);
    if (testBus->endTransmission() != 0) return false;
    distanceRegisterRead();
    return true;
  }
  bool available() {
    distanceRegisterRead();  // checkForDataReady / getResult
    return distanceHasNewSample;
  }
  int get() { return testDistanceMillimeters; }
};
