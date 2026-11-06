#pragma once

#include <cstdio>
#include <string>

// Host equivalents for the Arduino core types the sketch uses.
class String : public std::string {
 public:
  using std::string::string;
  void toCharArray(char* buffer, size_t size) const {
    std::snprintf(buffer, size, "%s", c_str());
  }
};

inline void delay(unsigned long) {}
