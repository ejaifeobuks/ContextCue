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

// Qwiic bus: only the Distance address probe is modeled.
struct TestWire {
  int address = -1;
  void beginTransmission(int value) { address = value; }
  int endTransmission() { return address == 0x29 && !distanceReachable ? 2 : 0; }
};
inline TestWire Wire1;

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
