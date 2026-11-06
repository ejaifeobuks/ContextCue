# ContextCue Conflict Resolution Review

## Purpose

This document records the differences found between the earlier
**ContextCue Prototype Build, Demo and User Experience Plan** and the later
**ContextCue Software Baseline v1**.

It records decisions made during review and keeps unresolved questions visible.
The software baseline remains the authority for safety, validation, freshness,
command limits, and hardware behavior unless a later decision formally changes
it.

## Review status

| Item | Status |
|---|---|
| Motion versus Distance | Resolved |
| Camera gesture recognition | Resolved |
| LLM requirement | Resolved |
| Calendar and weather sources | Partially resolved |
| Live-source setup behavior | Resolved |
| Separate demo light | Resolved |
| Physical command policy | Resolved |
| Physical Knob versus touchscreen controls | Resolved |
| Physical Buttons versus touchscreen controls | Resolved |
| Proximity and audio routing | Resolved with configurable threshold |
| Away/clear audio behavior | Resolved with configurable long-idle interval |

---

## 1. Motion sensor versus Distance sensor

### Difference

The earlier prototype plan uses a motion sensor to influence wake behavior and
notification prominence.

The later software baseline replaces older motion/PIR wording with a calibrated
time-of-flight Distance sensor. It defines near and clear distance thresholds
and explicitly prohibits inferring identity, occupancy, attention, sleep, or
absence.

### Decision

Use **Distance only** for proximity behavior.

Distance may:

- Wake the user interface.
- Represent that an object is near the device.
- Provide a hint to notification routing.

Distance must not:

- Identify a person.
- Prove that a person is present or absent.
- Infer occupancy, attention, sleep, or identity.
- Be treated as a safety or emergency detector.

### Baseline behavior

- Near: `<=500 mm`
- Clear: `>650 mm`
- Near persistence: `500 ms`
- Out-of-range: unknown
- Distance target sampling: `10 Hz`
- Distance stale after: `2 seconds`

---

## 2. Camera gesture recognition

### Difference

The earlier prototype plan describes camera gesture recognition as an optional
stretch feature. The later baseline defers camera capture, inference, gestures,
speech recognition, and wake word behavior from the core product path.

### Decision

Keep camera gestures as a **stretch feature**.

The only currently approved gesture action is:

- Acknowledge a notice.

Gesture recognition must remain:

- Outside the core product path.
- Disabled by default.
- Behind a reliability and safety gate.
- Unable to execute hardware commands.
- Unable to change notification policy without explicit supported behavior.

If the reliability gate fails, the feature is removed from the demonstration
without affecting the rest of the product.

---

## 3. Optional language model versus required LLM generation

### Difference

The earlier prototype plan treats the local language model as optional. It
allows deterministic templates to carry the product if the model is unreliable.

The later baseline requires actual LLM-generated briefings. It still requires a
clearly labeled factual fallback when generation fails, but the fallback does
not satisfy final LLM-generation acceptance.

### Decision

Use **LLM-generated briefings** as the intended briefing and recommendation
behavior.

The LLM will receive a current, validated context snapshot and may write a
concise response across multiple scenarios, such as:

- Morning briefing
- Upcoming event preparation
- Rain-related preparation
- Indoor comfort
- Bounded context questions

The LLM must not invent facts, change evidence, or control hardware. Its output
must be checked against the source evidence before publication.

Deterministic templates remain useful as a clearly labeled failure fallback,
but a template fallback does not count as successful LLM-generation acceptance.

### Remaining implementation decisions

- Select and benchmark the local model and runtime.
- Measure latency and memory usage on the target UNO Q.
- Define the generation timeout and output limits.
- Define the exact factual-fallback format.

---

## 4. Live calendar/weather providers versus fixed fixtures

### Difference

The earlier prototype plan allows setup to offer Demo data or a Live provider.

The later baseline prioritizes visibly labeled deterministic fictional fixtures
and defers live providers unless a real adapter exists.

### Decision

Support **fixed/demo and live sources side by side**.

Fixed sources will be used for the Qualcomm demonstration because they provide
repeatable scenarios and controlled failures. Live sources will remain
available for normal use and integration testing.

The initial live calendar scope is:

- Google Calendar
- Microsoft 365 / Outlook Calendar

Each provider will have its own adapter, but both adapters must return the same
ContextCue calendar contract.

Live adapters must:

- Use the same application-facing contracts as fixed sources.
- Include source and freshness metadata.
- Mark stale or unavailable data explicitly.
- Never silently fall back to fabricated values.
- Never allow indoor sensors to substitute for outdoor forecasts.

### Additional decision

Use **Open-Meteo** as the selected initial live weather provider.

### Authentication direction

Use Clerk for the application identity and Google/Microsoft OAuth connection
flow. Clerk can provide the provider OAuth access token to the backend after the
user authorizes the required calendar scopes.

Clerk is an authentication and OAuth layer, not a calendar adapter. The
application still needs separate `GoogleCalendarAdapter` and
`Microsoft365CalendarAdapter` implementations that call Google Calendar or
Microsoft Graph and translate responses into the shared ContextCue contract.

Live calendar setup is **optional during onboarding**:

- The user can continue with fixed/demo sources.
- The user may choose “Connect Google Calendar” or “Connect Microsoft 365.”
- The user must explicitly authorize calendar access.
- Calendar tokens and provider secrets must remain backend-only.
- The application must continue working when no live calendar is connected.
- The Qualcomm demonstration uses fixed/demo data and does not require Clerk.

Clerk custom provider credentials and calendar scopes must be configured for
production. Provider consent, verification, token expiry, revocation, and
reauthorization must be tested before treating live integration as complete.

### Weather-provider options

Implement an `OpenMeteoWeatherAdapter` using the shared ContextCue weather
contract. Verify coverage, usage terms, attribution requirements, freshness,
rate handling, timeout behavior, malformed responses, and offline recovery.

Keep `FixtureWeatherAdapter` for the Qualcomm demonstration, repeatable tests,
and controlled failure scenarios. Open-Meteo failures must remain visible and
must never silently become simulated values.

### UNO Q placement

Calendar integration can run on the UNO Q, but specifically on its Linux
processor side:

```text
UNO Q Linux
  -> Clerk/OAuth
  -> Google Calendar or Microsoft Graph
  -> provider adapter
  -> shared ContextCue calendar contract
  -> ContextEngine
```

The MCU side should not handle OAuth, account tokens, or calendar data. It
remains responsible for real-time sensors, inputs, and bounded Pixel/Buzzer
commands. The Qualcomm demonstration continues to use fixed/demo sources and
does not require Clerk or an internet connection.

---

## 5. Separate demo light versus selected outputs

### Difference

The earlier prototype plan includes a possible demonstration action such as
“Turn on the demo light.” It also lists a separate demonstration light or
buzzer as optional hardware.

The later baseline selects Pixels and Buzzer as the standard outputs and does
not make a separate lamp a mandatory deliverable.

### Decision

Use **Pixels and Buzzer only** for the selected output hardware.

Do not add a separate demo light to the required product path.

---

## 6. Physical Knob versus touchscreen controls

### Difference

The earlier design assigned speech-volume adjustment and the active notice's
Why action to a physical Knob.

The revised design removes the Knob from the selected hardware. The
touchscreen already provides a user-facing control surface and can handle
volume, navigation, acknowledgement, mute, Focus, and the Why view.

### Decision

Do not include a physical Knob.

The touchscreen will:

- Adjust selected Bluetooth speaker/headphone speech volume from 0% to 100%.
- Open the active notice's Why view.
- Provide touch equivalents for acknowledgement, mute, and Focus.

The MCU will no longer read or report Knob events. The Linux audio layer will
apply volume changes and report unsupported or failed endpoint operations
explicitly. Buzzer volume remains separately bounded and is not controlled by
the speech-volume setting.

---

## 7. Physical command policy

### Difference

The earlier prototype plan requires confirmation before physical actions and
requires bounded MCU commands.

The later baseline gives this behavior more precise safety rules:

- Allowlisted targets and actions
- Bounded parameters
- Session and command IDs
- Command expiration
- Duplicate rejection
- MCU-side validation
- Output lease and safe state

### Decision

Use **confirmed, allowlisted, bounded commands**.

Supported output actions should remain fixed, such as:

```text
pixels.set_pattern
pixels.off
buzzer.short_tone
buzzer.stop
outputs.stop
```

Model output must never directly issue arbitrary hardware commands.

The MCU must independently enforce command bounds even if Linux has already
validated the command.

The output lease should turn off Pixels and the Buzzer if Linux stops renewing
permission.

---

## 8. Proximity and audio routing

### Difference

The earlier prototype plan uses motion state as part of the decision about
whether to route a notification through Bluetooth audio.

The selected design uses Distance instead, but Distance cannot prove that a
person is present, absent, attentive, or wearing headphones.

### Decision

Use proximity only as a **routing hint**.

Audio delivery must still be controlled by all normal policy checks:

- User audio preference
- Notice severity
- Focus
- Mute
- Quiet hours
- Evidence freshness
- Notice validity and expiration
- Bluetooth endpoint readiness
- Acknowledgement and cancellation state

Proximity must not be the sole authority for audio delivery.

### Additional decision

- If Distance is clear and the device remains idle for the configured
  long-idle interval, suppress automatic speech and Buzzer output.
- Keep the notification visible on the screen and Pixels.
- Allow explicit playback later when the notice is still valid and eligible.
- Treat automatic Buzzer suppression as an audio-suppression condition; do not
  replace suppressed sound with another automatic sound.
- When the user appears away, Bluetooth audio may announce a reminder once only
  if the user explicitly enabled away-mode audio.
- When Bluetooth is disconnected, ordinary reminders queue silently and a
  return summary is shown later.
- Do Not Disturb suppresses ordinary sounds and collects reminders.
- Unanswered reminders do not repeat indefinitely; only explicitly important
  reminders may use a bounded repeat count.

This is a conservative “not currently detected” behavior, not a claim that the
user is absent or asleep. The exact long-idle interval and unknown-Distance
behavior remain implementation parameters to define.

The system must never claim that a user heard audio merely because playback
was attempted or a Bluetooth endpoint was connected.

---

## 8. Physical Buttons versus touchscreen controls

The selected hardware no longer includes the physical Buttons Modulino. The
touchscreen is the only user input surface and provides acknowledgement, mute,
Focus, Why navigation, and speech-volume control.

This removes duplicate control paths and avoids spending a Modulino on actions
that the touchscreen already supports. The MCU no longer needs button event
retention or a local button-driven buzzer stop path.

The Buzzer remains an output and continues to obey mute, Focus, quiet hours,
automatic-audio suppression, bounded-duration, and output-lease rules.

---

## 9. Overall reconciliation

The earlier prototype plan and later baseline share the same broad product
vision:

- Combine schedule, weather, and room context.
- Show explainable recommendations.
- Use screen, Pixels, and Bluetooth audio.
- Continue operating during external failures.
- Keep simulation visible.
- Separate Linux application behavior from MCU hardware control.
- Require confirmation and safe command boundaries.

The later baseline is stricter about:

- Distance semantics.
- Camera scope.
- LLM acceptance.
- Source validity and freshness.
- Notice lifecycle.
- Hardware command validation.
- Output leases.
- Model and audio cancellation.

The current implementation should follow the resolved decisions in this
document while keeping unresolved items out of irreversible interfaces until
they are decided.

## Reference documents

- [Software baseline](../requirements/ContextCue_Software_Baseline_v1.md)
- [UML class reference](../architecture/ContextCue_UML_Class_Reference.md)
- [Prototype build, demo and user experience plan](../background/ContextCue_Prototype_Build_Demo_and_User_Experience_Plan.docx)
- [Implementation plan](IMPLEMENTATION_PLAN.md)
