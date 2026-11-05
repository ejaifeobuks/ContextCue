#pragma once

#include <cstdint>
#include <functional>

inline int Wire1 = 1;
inline bool thermoReachable = true;
inline float testTemperature = 25.75f;
inline float testHumidity = 65.52f;
inline std::function<void(const char*)> duringAcquisition;

struct TestModulino { void begin(int) {} };
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
