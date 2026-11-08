#include "DivideByZeroTrap.h"

#include <stdint.h>

namespace contextcue {
namespace {

int depth = 0;

#if defined(ARDUINO_ARCH_ZEPHYR)
// System Control Block Configuration and Control Register (Armv8-M).
volatile uint32_t* const kCcr = reinterpret_cast<volatile uint32_t*>(0xE000ED14);
constexpr uint32_t kDivideByZeroTrap = 1u << 4;  // CCR.DIV_0_TRP

void barrier() { __asm volatile("dsb\n\tisb" ::: "memory"); }
#endif

}  // namespace

DivideByZeroTrapOff::DivideByZeroTrapOff() {
#if defined(ARDUINO_ARCH_ZEPHYR)
  wasOn_ = (*kCcr & kDivideByZeroTrap) != 0;
  *kCcr &= ~kDivideByZeroTrap;
  barrier();
#endif
  ++depth;
}

DivideByZeroTrapOff::~DivideByZeroTrapOff() {
  --depth;
#if defined(ARDUINO_ARCH_ZEPHYR)
  if (wasOn_) {
    *kCcr |= kDivideByZeroTrap;
    barrier();
  }
#endif
}

int divideByZeroTrapOffDepth() { return depth; }

}  // namespace contextcue
