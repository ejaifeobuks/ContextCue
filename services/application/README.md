# Application service — planned, not implemented

The single application backend (baseline §1: one node, no microservices). Owns
the SWE domain classes:

- **ContextEngine** — fixtures and live calendar/weather adapters, sensor
  normalization, schedule calculation, freshness and trigger rules, immutable
  context snapshots.
- **NotificationService** — notice priority, delivery policy, mute/Focus,
  acknowledgement, local TTS and the audio queue.
- **DataRepository** — settings, evidence, acknowledgements, diagnostics and
  retention.

Uses the `contextcue-hardware` app's `HardwareGateway` in-process for readings, health
and approved output commands, and the AI service for grounded advice. Uses
the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md),
[implementation plan](../../docs/planning/IMPLEMENTATION_PLAN.md) phases 9 and 10.
