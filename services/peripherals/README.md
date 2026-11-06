# Peripherals service — planned, not implemented

Owns **PeripheralAdapter** (hardware software, Linux): display and USB touch
initialization, brightness capability, and Bluetooth speaker discovery,
pairing, readiness, playback and stop (baseline HW-02, HW-05).

- Used by `HardwareGateway` (see [apps/contextcue-hardware](../../apps/contextcue-hardware/)); it does not
  interpret model advice or queue notice retries.
- Runs only on the UNO Q Linux side; it has no MCU firmware.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md) phases 6 and 8.
