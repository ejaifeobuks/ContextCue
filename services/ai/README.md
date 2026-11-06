# AI service — planned, not implemented

Owns **RecommendationService** (SWE): the isolated, asynchronous, bounded LLM
worker (baseline APP-06 to APP-08).

- Receives immutable `ContextSnapshot` requests from ContextEngine and returns
  `ModelResponse` results that reference current evidence IDs.
- Validates schema, category, evidence and numeric facts; rejects obsolete
  results; produces labeled factual fallbacks; supports cancellation.
- Has no hardware authority and no silent cloud fallback.
- Uses the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md).
