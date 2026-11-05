#pragma once

#include <cstdio>
#include <string>

class String : public std::string {
 public:
  using std::string::string;
  void toCharArray(char* buffer, size_t size) const {
    std::snprintf(buffer, size, "%s", c_str());
  }
};

struct TestBridge {
  void begin() {}
  template<typename F> void provide_safe(const char*, F) {}
};
inline TestBridge Bridge;
inline void delay(unsigned long) {}
