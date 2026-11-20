# ContextCue Security and Privacy Review

**Status:** Preliminary review; findings open, fixes not applied.  
**Method:** AI-assisted source inspection and local tests against the supplied repository snapshot.  
**Snapshot:** `IEEE_Stuff-master.zip`, SHA-256 `898c80add324473095fe356e7524d9d22acf8443590dfb85537995c45afd9048`. No Git history or commit identifier was supplied.

## Summary

ContextCue has useful safeguards: local model execution by default, constrained AI output, factual fallbacks, evidence freshness checks, bounded hardware outputs, and calendar-link masking in the UI. However, local AI does not by itself protect the HTTP interface or stored calendar data.

The highest-priority concern is the combination of an unauthenticated API and a board launcher that binds to all network interfaces. Actual reachability depends on container publishing, host firewall rules, and network configuration, which were not verified. Other findings concern calendar URL restrictions, plaintext secret storage, data deletion expectations, and incomplete semantic checking of AI output.

This is a scoped assessment, not a penetration test, certification, or guarantee of security. Priorities below are qualitative and depend on deployment; no CVSS scores were calculated.

## Scope and limits

Inspected the UI HTTP handler and launcher, application actions, calendar adapter, SQLite repository, recommendation validation and fallback logic, selected settings and output-gateway code, and existing tests. Used only synthetic data in additional checks. No production calendar, external target, or board was accessed.

Not assessed: running-board firewall/container exposure, authentication supplied by an external proxy, OS permissions and disk encryption, firmware security as a whole, dependency vulnerabilities, complete repository history, or a comprehensive secret scan. No malicious calendar was sent to the model, so successful prompt injection is not claimed.

## Data and trust boundaries

- Calendar subscription URL → HTTPS download → calendar event data.
- Room sensors and weather → application evidence and deterministic rules.
- Selected evidence, including event titles/locations → model prompt → validated text or factual fallback.
- HTTP browser/API client → application actions, settings, and state.
- Application → bounded hardware gateway; the inspected model interface exposes text rather than hardware tools.
- Settings, evidence, notices, diagnostics, and calendar link → local SQLite storage.

Calendar titles, locations, and generated text may contain personal information even when processing stays on the board. Calendar feed content is untrusted input, including when it comes from a legitimate provider.

## Findings

### CC-01 — API access has no authentication check

**Priority:** High if reachable by untrusted clients; reduced on an isolated kiosk.  
**Evidence:** `apps/contextcue/python/main.py` launches the UI with `--host 0.0.0.0 --port 7000`. In `services/ui/contextcue_ui/server.py`, `do_GET` serves `/api/state` and `do_POST` dispatches `/api/action` without authenticating the caller. `services/application/contextcue_app/app.py` exposes actions for settings, calendar connection/disconnection, and clearing data. The handler also has no Origin/Host validation or CSRF token check.

**Impact:** A client that can reach the service can request application state or invoke supported actions. The Python HTTP handler supplies no TLS, so an unprotected network connection can expose request bodies, including a submitted calendar link. This does not establish that the service is publicly reachable. Cross-origin exploitation from a browser was not tested; absent CORS headers should not be treated as API authentication.

**Verification:** Static inspection plus existing UI tests, which successfully read state and invoke actions without credentials on a loopback test server. No remote device was probed.

**Recommendation:** Prefer loopback-only binding for the kiosk and verify container port publishing. If remote access is required, use authenticated HTTPS, authorize sensitive actions, validate Origin/Host, add CSRF protection where applicable, and restrict network access. Test that unauthenticated requests cannot read state or change settings.

### CC-02 — Calendar fetch destinations are insufficiently restricted

**Priority:** Medium; increased by CC-01 if untrusted clients can submit links.  
**Evidence:** `normalize_link` in `services/application/contextcue_app/calendar_ics.py` requires HTTPS and a nonempty network location, but does not reject loopback/private destinations or URL credentials. `_download` uses `urllib.request.urlopen`, whose default redirect handling is not restricted here. `_act_calendar_connect` downloads a candidate link before storing it.

**Impact:** A submitted URL can cause the application to attempt requests to unintended HTTPS destinations, potentially including internal services. HTTPS alone does not establish that a destination is safe. A non-calendar response is rejected only after the request. This review did not demonstrate internal data exfiltration, redirect exploitation, or access to a live internal service.

**Verification:** A local call to `normalize_link('https://127.0.0.1/calendar.ics')` accepted the address. No network request was made in this check.

**Recommendation:** Reject embedded credentials and non-public destination addresses, check all resolved IPv4/IPv6 addresses and redirect targets, and preserve these restrictions through connection establishment to avoid DNS-rebinding gaps. Consider an explicit provider allowlist where compatible with project requirements. Add tests for loopback, private/link-local addresses, redirects, and DNS changes.

### CC-03 — Calendar bearer link is stored as plaintext

**Priority:** Medium, conditional on database or backup access.  
**Evidence:** `DataRepository.save_secret` in `services/application/contextcue_app/repository.py` writes the full link directly to the SQLite `meta` table. Separating it from normal settings prevents routine UI exposure but does not encrypt it. The inspected repository code does not explicitly set restrictive file permissions; actual OS protection remains unverified.

**Impact:** Someone who obtains the database or relevant backups can recover the calendar link and potentially read the subscribed calendar. Local event and notice records can also reveal personal information.

**Verification:** In a temporary database, a synthetic link was readable through a direct SQLite query. No real secret was used.

**Recommendation:** Restrict application directory and database permissions, protect backups and database sidecar files, and use an OS-backed secret store or appropriately managed encryption where available. A key stored beside the database is not sufficient protection against database-directory compromise. Document how to revoke/rotate the calendar subscription URL.

### CC-04 — Clear data preserves the calendar connection secret

**Priority:** Low privacy/usability risk; relevant when handing the device to someone else.  
**Evidence:** `_act_clear_data` calls `clear_data(keep_settings=True)`. `DataRepository.clear_data` clears evidence, notices, tombstones and diagnostics but leaves `meta`, including `secret:calendar_link`. The separate calendar-disconnect action deletes the secret row.

**Impact:** A user may believe all personal data was removed while the device retains its calendar connection and can fetch events again. This is intentional behavior in code, but should be explicit in the UI. Deleting a SQLite row is not a guarantee of forensic erasure from storage or backups.

**Verification:** A synthetic secret survived `clear_data(keep_settings=True)` in a temporary database.

**Recommendation:** Explain exactly what “Clear data” preserves. Provide a separate confirmed “Disconnect calendar and reset personal data” operation for device transfer, and document backup and storage-erasure limits.

### CC-05 — AI validation checks numbers without fully checking their meaning

**Priority:** Medium integrity risk for user-facing advice; not demonstrated code execution.  
**Evidence:** `_allowed` and `validate_response` in `services/application/contextcue_app/recommendation.py` collect allowed numeric values across evidence. They do not fully associate every generated claim with the correct metric or forecast period.

**Verification:** A synthetic snapshot contained 10% rain now and 70% later. The message “There is a 70% chance of rain now and 10% later.”, referencing both evidence items, returned `None` from `validate_response`, meaning accepted. This reproduced a semantic validation gap without model execution.

**Impact:** A message can cite valid evidence and contain only supported numbers while assigning them to the wrong time or meaning. This is a correctness weakness; malicious exploitation was not demonstrated.

**Recommendation:** Compute time comparisons and recommendations deterministically. Prefer structured semantic fields checked against specific evidence before rendering text, or deterministic templates for consequential claims. Add regression cases for reversed periods, swapped temperature/humidity, and AM/PM changes. Treat calendar text as data rather than instructions and test adversarial titles separately.

**Benchmark caution:** `experiments/thinking/README.md` reports nine wrong-but-shown production-configuration answers in 34 historical runs. The supplied current validator already contains time, comfort, and repetition checks discussed as follow-up work in that report. The historical count is not a measurement of the current snapshot and was not rerun here.

## Safeguards observed

- UI server defaults to loopback when not overridden; static file paths are resolved and checked against the asset directory.
- JSON responses use `Cache-Control: no-store`; action requests have a 16 KB body limit.
- Calendar downloads have a 15-second timeout and 5 MB response limit. Known download errors avoid returning the secret URL.
- Calendar connection state exposes the host and connected status rather than the full subscription link.
- SQLite statements use parameters for values; diagnostic/evidence serialization redacts selected secret-like keys. This is not general personal-data anonymization.
- Recommendation generation is bounded and supports cancellation, validation, and factual fallback. Current validation includes explicit time checks, comfort contradictions, and repeated phrases.
- The model client has no tool interface; gateway output controls and lease handling provide additional boundaries. Hardware behavior was not exercised in this review.

## Tests and reproducibility

Run from the repository root:

```sh
python -m unittest discover -s tests/application -t . -q
python -m unittest tests.ui.test_server -q
```

Results in this review environment:

| Check | Result |
|---|---|
| Existing application tests | 122 run: 114 passed, 2 failed, 6 errors. The eight unsuccessful cases were calendar-related and occurred with `icalendar` unavailable; these are not classified as verified product defects. |
| Existing UI server tests | 3 passed. |
| Synthetic URL normalization | Loopback HTTPS accepted; no network access attempted. |
| Synthetic database check | Plaintext secret readable and retained after clear-data. |
| Synthetic AI validator check | Reversed now/later rain percentages accepted. |

Install the project's declared calendar dependencies and rerun calendar tests before drawing conclusions about their behavior. These tests are not a complete security test suite.

To reproduce CC-05 using existing test helpers:

```python
import sys
sys.path[:0] = ['tests/application', 'services/application',
                'apps/contextcue-hardware/python']
from test_recommendation_repository import evidence, snapshot, response
from contextcue_app.recommendation import validate_response

ev = (
    evidence('weather.now', 'forecast_next',
             {'condition': 'cloudy', 'precipitation_probability': 10.0}),
    evidence('weather.later', 'forecast_later',
             {'condition': 'rainy', 'precipitation_probability': 70.0,
              'start_local': '17:00', 'end_local': '18:00'}),
)
snap = snapshot('context_answer', ev, {}, 'rain_today')
reply = response('There is a 70% chance of rain now and 10% later.',
                 ('weather.now', 'weather.later'), 'context_answer')
print(validate_response(reply, snap))  # None: accepted in reviewed snapshot
```

## Recommended next steps

1. Confirm and restrict the running board's port exposure; protect remote API access if needed.
2. Restrict calendar fetch destinations and redirects.
3. Protect stored calendar credentials and clarify reset/disconnect behavior.
4. Add semantic validator regression tests and evaluate prompt injection with synthetic calendar content.
5. Rerun calendar tests with dependencies installed, then verify fixes on the board using synthetic data.

