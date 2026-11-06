# UI service — planned, not implemented

Owns **UserInterface** (SWE): the kiosk touch UI with Glance/Dashboard,
Briefing/Alert, Why, Settings/Focus, Audio, Health and Simulation views
(baseline APP-02, APP-03, APP-09).

- Sends questions and settings to the application service (ContextEngine).
- Sends acknowledge, mute and Focus actions to NotificationService; receives
  notices and status from it.
- Holds only view state (`currentView`, `selectedNotice`); it is not a
  notification authority and never talks to hardware directly.
- Uses the `contextcue_contracts` package (today in `apps/contextcue-hardware/python/`) for every cross-service type.

References: [UML class reference](../../docs/architecture/ContextCue_UML_Class_Reference.md),
[software baseline](../../docs/requirements/ContextCue_Software_Baseline_v1.md).
