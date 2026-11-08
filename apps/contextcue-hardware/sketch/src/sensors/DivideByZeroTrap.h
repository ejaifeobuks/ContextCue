#pragma once

namespace contextcue {

// Arduino_LTR381RGB 1.0.1 (Modulino Light) computes, in getALS():
//
//   lux = ((0.8f*rawlux)/(gain*intTime))*(1 - (0.033f*(ir/rawlux)));
//
// where `ir/rawlux` is integer division. In darkness the raw ALS count is 0.
// Zephyr enables the Cortex-M33 divide-by-zero trap (SCB->CCR.DIV_0_TRP), so
// that division raises a UsageFault and Zephyr halts the whole MCU: the
// Bridge stops, every RPC times out, and a reset in the same darkness halts it
// again. Diagnosed over SWD on 8 November 2026 (IPSR 6, sdiv r3, r3, r1 with
// r1 = 0 in LTR381RGBClass::getALS).
//
// With the trap off, SDIV/UDIV by zero return 0 (Armv8-M), so the library
// reports 0 lux, which is what a dark sensor measures. The trap is off only
// for the scope of this object and restored afterwards.
class DivideByZeroTrapOff {
 public:
  DivideByZeroTrapOff();
  ~DivideByZeroTrapOff();
  DivideByZeroTrapOff(const DivideByZeroTrapOff&) = delete;
  DivideByZeroTrapOff& operator=(const DivideByZeroTrapOff&) = delete;

 private:
  bool wasOn_ = false;
};

// Number of live DivideByZeroTrapOff scopes (host tests check it).
int divideByZeroTrapOffDepth();

}  // namespace contextcue
