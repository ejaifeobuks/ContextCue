# ContextCue UML class reference

Supporting document | Version 1.0 | 25 October 2026

This reference explains the 11 classes in the ContextCue UML diagram so the software teams can implement the same design. Names match the diagram exactly. It introduces no additional components.

## Reading the diagram

- The top compartment names the class and owner; the middle lists private attributes (`-`); the bottom lists public operations (`+`).
- Solid arrows indicate use/dependency; dashed arrows indicate test dependencies. They do not specify inheritance, object ownership or a network protocol.
- Empty parentheses are abbreviated operation names, not a requirement for zero arguments. Define typed parameters/results during interface implementation, including relevant request, notice, event and command IDs.
- Linux-to-MCU relationships cross Arduino Bridge; they are not ordinary in-process object calls. Keep hardware access behind HardwareGateway.

Runtime state has clear ownership: NotificationService owns mute/Focus; ContextEngine consumes synchronized settings; DataRepository stores the persisted copy. UserInterface.selectedNotice is a view selection, while NotificationService.activeNotice is the delivery authority.

## 1 UserInterface

**Owner:** SWE team

**Purpose:** Presents information and collects user actions. It delegates product decisions and device operations to the responsible classes.

**Attributes**

- `currentView` - The screen currently displayed, such as Glance, Why or Settings.
- `selectedNotice` - The notice currently selected for viewing or interaction; not a separate notification authority.

| Method | Responsibility |
|---|---|
| `showDashboard()` | Display time, schedule, readings, mode and highest-priority notice. |
| `showBriefing()` | Display the selected briefing, including generation or fallback status. |
| `showWhy()` | Show supporting evidence, sources, timestamps, freshness and simulation labels. |
| `showSettings()` | Display preferences and collect requested changes. |
| `showDeviceHealth()` | Display sensor, model, connection and audio availability. |
| `submitQuestion()` | Forward a typed context question to ContextEngine. |

**Relationships:** Sends questions/settings to ContextEngine and acknowledgement, mute and Focus actions to NotificationService. Receives notices/status from NotificationService; device health reaches it through application state.

## 2 ContextEngine

**Owner:** SWE team

**Purpose:** Builds the current context and decides when a situation is eligible for a cue using deterministic rules.

**Attributes**

- `currentSnapshot` - A versioned view of evidence, calculated facts and applicable preferences.
- `userSettings` - The working preference values used for calculations and trigger evaluation.

| Method | Responsibility |
|---|---|
| `loadCalendarWeather()` | Load labeled calendar/forecast fixtures and preserve their validity metadata. |
| `processSensorReadings()` | Normalize valid readings, update context and mark unavailable sources. |
| `calculateSchedule()` | Calculate next event, event count, urgency and non-duplicated scheduled duration. |
| `checkFreshness()` | Determine whether each source remains valid for the intended use. |
| `evaluateTriggers()` | Apply proximity, comfort, schedule, persistence and cooldown rules. |
| `buildSnapshot()` | Create immutable evidence and calculated facts for a model request. |
| `handleUserInput()` | Map input events or questions to the appropriate application action. |

**Relationships:** Uses HardwareGateway for readings, controls and health, and DataRepository for evidence/preferences. Requests advice from RecommendationService and submits eligible notices to NotificationService. Simulator supplies labeled test context.

## 3 RecommendationService

**Owner:** SWE team

**Purpose:** Produces bounded LLM advice and factual fallback, with validation and cancellation. It has no hardware or notification-policy authority.

**Attributes**

- `model` - The configured inference model/runtime handle and its execution state.
- `generationDeadline` - The cutoff for the active generation request, enforced without blocking controls.

| Method | Responsibility |
|---|---|
| `generateBriefing()` | Generate a short morning, preparation or comfort recommendation from a snapshot. |
| `answerContextQuestion()` | Answer a bounded question using only available context. |
| `validateEvidence()` | Check output structure, category, evidence references and supported facts. |
| `rejectObsoleteResult()` | Discard output whose relevant evidence or request state is no longer valid. |
| `generateFactualFallback()` | Build labeled factual text from still-valid inputs when generation fails. |
| `cancelGeneration()` | Stop or invalidate pending generation so late results cannot publish. |

**Relationships:** ContextEngine supplies snapshots and receives results for further eligibility handling. Simulator injects model-failure cases. Current evidence/request state must be supplied for revalidation; the diagram does not imply direct database or hardware access.

## 4 NotificationService

**Owner:** SWE team

**Purpose:** Owns notice selection, delivery policy and cancellation across screen, pixels, buzzer and Bluetooth speech.

**Attributes**

- `activeNotice` - The selected notice with identity, evidence, expiry and acknowledgement state.
- `audioQueue` - Pending speech items tied to notice IDs and revisions.
- `muted` - Authoritative runtime flag suppressing speech and buzzer output.
- `focusEnabled` - Authoritative runtime Focus flag restricting interruptions.

| Method | Responsibility |
|---|---|
| `selectPriorityNotice()` | Select the highest-priority eligible notice using deterministic tie-breaking. |
| `applyDeliveryPolicy()` | Check freshness, expiry, Focus, quiet hours, mute and endpoint readiness. |
| `publishNotice()` | Update the UI and dispatch only permitted output requests. |
| `generateSpeech()` | Synthesize local speech from approved notice text and queue it. |
| `acknowledgeNotice()` | Clear that notice, stop its audio, cancel repeats and persist acknowledgement. |
| `setMute()` | Update mute state; stop/suppress audio while retaining required visuals. |
| `setFocus()` | Update Focus and re-evaluate deferred notices and permitted routes. |
| `cancelExpiredOutputs()` | Remove or stop expired, obsolete or acknowledged queued/active outputs. |

**Relationships:** Receives candidates from ContextEngine and user controls from UserInterface. Sends notices/status to UserInterface, approved outputs through HardwareGateway, and acknowledgement/log records to DataRepository.

## 5 DataRepository

**Owner:** SWE team

**Purpose:** Provides persistent local storage and retention behavior for settings, evidence and notice records.

**Attributes**

- `settings` - Persisted configuration and user preferences.
- `savedEvidence` - Stored source values and provenance needed for Why and valid recovery.
- `noticeHistory` - Notice records, acknowledgement markers and diagnostic history.

| Method | Responsibility |
|---|---|
| `saveSettings()` | Persist validated preference changes transactionally. |
| `restoreSettings()` | Load persisted preferences or defined defaults at startup. |
| `saveEvidence()` | Store evidence with source, validity and observation metadata. |
| `saveAcknowledgement()` | Persist the notice ID/revision so it cannot replay after restart. |
| `writeDiagnosticLog()` | Record structured operational events while excluding secrets and raw media. |
| `removeExpiredData()` | Apply retention limits without removing still-required acknowledgement protection. |

**Relationships:** ContextEngine uses it for context/preferences; NotificationService uses it for acknowledgement and diagnostic records. Restoring evidence never makes an old observation fresh.

## 6 Simulator

**Owner:** SWE team - testing

**Purpose:** Exercises production contracts with reproducible labeled inputs and controlled faults, without requiring the complete hardware rig.

**Attributes**

- `scenario` - The selected fixture set, fault plan and expected outcomes.
- `virtualClock` - Controllable scenario time for expiry, cooldown and scheduling tests.

| Method | Responsibility |
|---|---|
| `loadScenario()` | Load scenario inputs, initial settings and expected outcomes. |
| `provideSimulatedReadings()` | Provide labeled samples in the same shape as hardware readings. |
| `advanceTime()` | Move scenario time to test deadlines and duration-based behavior. |
| `simulateDeviceFailure()` | Inject sensor, Bridge or peripheral failure into the test environment. |
| `simulateModelFailure()` | Inject generation timeout, error, malformed output or unsupported content. |
| `checkExpectedResults()` | Compare observed policy, notices and route outcomes with expectations. |

**Relationships:** Dashed dependencies target ContextEngine, RecommendationService and HardwareGateway for testing. Simulation must not silently replace real input in production or issue unintended commands to physical devices.

## 7 HardwareGateway

**Owner:** Hardware software team - Linux

**Purpose:** Provides the application-facing device boundary on Linux, hiding Bridge transport and peripheral integration details.

**Attributes**

- `connectedModules` - Discovered module identities, capabilities and availability.
- `connectionState` - Bridge/device session state used for health and recovery.
- `firmwareVersion` - Reported MCU firmware version used for compatibility checks.

| Method | Responsibility |
|---|---|
| `discoverDevices()` | Discover configured devices and report capabilities or missing modules. |
| `readSensorData()` | Retrieve samples and preserve sequence, validity and observation metadata. |
| `sendOutputCommand()` | Route approved bounded output requests and report command outcomes. |
| `configurePeripheral()` | Apply supported display/audio setup requests through PeripheralAdapter. |
| `getDeviceHealth()` | Aggregate device, Bridge and peripheral health for the application. |
| `reconnectBridge()` | Reconnect, detect resets, invalidate stale state and resynchronize safely. |

**Relationships:** ContextEngine and NotificationService are its application clients. It communicates with SensorController and OutputController through Bridge, and uses PeripheralAdapter for Linux devices. Simulator tests its contract/recovery behavior.

## 8 SensorController

**Owner:** Hardware software team - MCU

**Purpose:** Acquires and validates the three selected sensing modules on the MCU; it reports measurements rather than product conclusions.

**Attributes**

- `thermo` - The Thermo driver/measurement state for temperature and humidity.
- `light` - The ambient-light driver/measurement state.
- `distance` - The ToF distance driver/measurement state.

| Method | Responsibility |
|---|---|
| `readTemperatureHumidity()` | Acquire temperature and relative humidity with units and observation metadata. |
| `readAmbientLight()` | Acquire the ambient value with verified units or raw-value labeling. |
| `readDistance()` | Acquire distance in millimeters with validity/out-of-range status. |
| `validateReading()` | Check communication status, bounds and sample validity. |
| `reportSensorFailure()` | Report a missing, invalid or unavailable sensor without fabricating values. |

**Relationships:** Supplies readings and failures to HardwareGateway. ContextEngine interprets them as comfort, brightness or proximity evidence; this class does not infer identity or room occupancy.

## 9 OutputController

**Owner:** Hardware software team - MCU

**Purpose:** Executes bounded MCU output commands and enforces local limits even when the Linux application becomes unavailable.

**Attributes**

- `pixels` - The Pixels driver and current pattern/brightness state.
- `buzzer` - The Buzzer driver and current tone state.
- `outputLease` - The expiry of application permission to maintain outputs, renewed by heartbeat.

| Method | Responsibility |
|---|---|
| `setPixelPattern()` | Apply an allowlisted fixed pixel pattern. |
| `setPixelBrightness()` | Apply brightness within the supported bounds. |
| `playBoundedTone()` | Play an allowed short tone with a maximum duration. |
| `stopBuzzer()` | Immediately cancel the current tone and pending local tone work. |
| `validateCommand()` | Check action, parameters, expiry/session and duplicate-command handling. |
| `applyWatchdogSafeState()` | Silence the buzzer and turn pixels off when the output lease expires. |

**Relationships:** Receives bounded commands from HardwareGateway. It does not decide notice priority or bypass application mute/Focus policy.

## 10 PeripheralAdapter

**Owner:** Hardware software team - Linux

**Purpose:** Integrates the display, touch input and Bluetooth audio with the Linux platform, reporting operational results to the gateway.

**Attributes**

- `touchscreen` - Display/touch device handles, orientation and brightness capabilities.
- `bluetoothEndpoint` - The selected paired speaker/headset and connection information.
- `audioState` - Operational readiness and playback state; distinct from user mute preference.

| Method | Responsibility |
|---|---|
| `initializeDisplayAndTouch()` | Initialize the screen/touch path and validate orientation and availability. |
| `setDisplayBrightness()` | Apply supported native brightness or report that adjustment is unsupported. |
| `discoverAndPairSpeaker()` | Discover supported endpoints and complete user-selected pairing. |
| `checkAudioReadiness()` | Distinguish paired, connected and actually ready for playback. |
| `playAudio()` | Play supplied local audio and report start/completion/failure. |
| `stopAudio()` | Cancel actual ongoing playback and report its stopped state. |
| `reportPeripheralFailure()` | Report display, touch or audio faults to HardwareGateway. |

**Relationships:** Used by HardwareGateway. NotificationService creates speech and decides eligibility; this adapter plays it. It must not interpret model advice, independently queue notice retries or claim playback proves the user heard it.

## Implementation rules shared by these classes

1. Keep model generation and speech asynchronous. UI controls, sensor handling and cancellation must remain responsive.
2. Carry source, timestamp, validity and real/simulated labels with evidence. Recheck relevant context before publishing or playing an output.
3. Acknowledgement clears one notice across routes; mute silences speech and Buzzer while retaining visuals. Cancellation must also invalidate late callbacks and pending work.
4. Reconnect and restart must not replay expired or acknowledged notices or old buzzer commands. Restore valid settings/state without automatically resuming audio.
5. MCU local stop covers its Buzzer; it cannot independently silence Linux Bluetooth playback if Linux fails. Driver completion is not independent proof of physical output or user receipt.
6. Simulator exercises the same contracts with labeled fixtures. Measure real hardware/model/audio behavior separately; a test double or factual fallback is not actual-generation acceptance.

## Related design files

[Rendered class diagram](ContextCue_Class_Diagram.png) | [Editable Mermaid source](ContextCue_Class_Diagram.mmd) | [Software requirements baseline](../requirements/ContextCue_Software_Baseline_v1.md)

This document clarifies responsibilities and operation semantics. The software baseline remains the reference for thresholds, timing, retention and acceptance criteria. Method signatures and programming-language types are implementation details not specified by the diagram.
