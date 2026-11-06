#pragma once

#include "Arduino.h"

struct TestBridge {
  void begin() {}
  template<typename F> void provide_safe(const char*, F) {}
};
inline TestBridge Bridge;
