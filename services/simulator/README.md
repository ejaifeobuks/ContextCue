# Simulator — planned, not implemented

Owns **Simulator** (SWE/testing): labeled scenarios, virtual time, simulated
readings and controlled device/model failures (baseline APP-19, INT-01).

- Exercises ContextEngine, RecommendationService and HardwareGateway through
  the same `contextcue_contracts` package used in production (today in
  `apps/contextcue-hardware/python/`).
- Never selected automatically at runtime; simulation labels must survive
  every stage. Today's gateway test peers live in [`tests/hardware`](../../tests/hardware/).

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md).
