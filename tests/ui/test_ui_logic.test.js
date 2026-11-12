// Unit tests for services/ui/assets/ui-logic.js (pure presentation rules).
// Run: node --test tests/ui/test_ui_logic.test.js  (also run from test_ui_assets.py when Node exists)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "../../services/ui/assets/ui-logic.js"));

test("weather icons map only from the five structured conditions", () => {
  const expected = { sunny: "sun", cloudy: "cloud", rainy: "cloud-rain", snowy: "cloud-snow", foggy: "cloud-fog" };
  for (const [condition, icon] of Object.entries(expected)) {
    const v = L.weatherVisual({ condition, quality: "valid" });
    assert.equal(v.icon, icon);
    assert.equal(v.known, true);
  }
});

test("missing, stale, invalid or unknown weather never shows a sun or rain icon", () => {
  const cases = [null, { condition: "sunny", quality: "stale" }, { condition: "sunny", quality: "invalid" },
                 { condition: "thunderstorm", quality: "valid" }, { condition: null, quality: "valid" }];
  for (const f of cases) {
    const v = L.weatherVisual(f);
    assert.equal(v.known, false, JSON.stringify(f));
    assert.ok(["cloud-off", "circle-help"].includes(v.icon), v.icon);
    assert.ok(v.text.length > 0);
  }
  assert.equal(L.weatherVisual(null).text, "No forecast source");
  assert.equal(L.weatherVisual({ condition: "sunny", quality: "stale" }).text, "Forecast out of date");
});

test("clear sky at night reads Clear with a moon, never a sun", () => {
  const clear = { condition: "sunny", quality: "valid" };
  assert.deepEqual(L.weatherVisual(clear, true), { icon: "moon", text: "Clear", known: true });
  assert.equal(L.weatherVisual(clear, false).icon, "sun");
  assert.equal(L.weatherVisual({ condition: "rainy", quality: "valid" }, true).icon, "cloud-rain");
  assert.equal(L.isNightHour(19), true);
  assert.equal(L.isNightHour(5), true);
  assert.equal(L.isNightHour(12), false);
  assert.equal(L.isNightHour(NaN), false);
});

test("12-hour clock formatting", () => {
  assert.equal(L.fmt12("00:05"), "12:05 AM");
  assert.equal(L.fmt12("09:30"), "9:30 AM");
  assert.equal(L.fmt12("12:00"), "12:00 PM");
  assert.equal(L.fmt12("14:05"), "2:05 PM");
  assert.equal(L.fmt12("23:59"), "11:59 PM");
  assert.equal(L.fmt12("not a time"), "not a time");
});

test("greeting and sky follow the displayed clock", () => {
  assert.equal(L.greeting("07:45"), "Good morning");
  assert.equal(L.greeting("13:20"), "Good afternoon");
  assert.equal(L.greeting("22:30"), "Good evening");
  assert.equal(L.skyPhase("22:30"), "night");
  assert.equal(L.skyPhase("09:38"), "day");
});

test("countdowns round up like expiry times", () => {
  assert.equal(L.until(12.5 * 60000), "In 13 min");
  assert.equal(L.until(75 * 60000), "In 1 h 15 m");
  assert.equal(L.until(120 * 60000), "In 2 h");
  assert.equal(L.until(0), "Now");
  assert.equal(L.until(null), null);
});

test("invalid or missing sensor readings are statuses, never zero", () => {
  assert.deepEqual(L.sampleView("temperature", { value: 22.5, quality: "valid" }, "metric").text, "22.5 °C");
  assert.equal(L.sampleView("temperature", { value: 22.5, quality: "valid" }, "imperial").text, "72.5 °F");
  assert.equal(L.sampleView("distance", { value: 428, quality: "valid" }).text, "428 mm");
  const bad = L.sampleView("ambient_light", { value: 0, quality: "invalid", error: "light_disconnected" });
  assert.equal(bad.ok, false);
  assert.equal(bad.text, "Unavailable");
  assert.equal(L.sampleView("humidity", { value: null, quality: "valid" }).text, "Unavailable");
  assert.equal(L.sampleView("humidity", { value: 40, quality: "stale" }).text, "Stale");
  assert.equal(L.sampleView("humidity", undefined).text, "No data yet");
});

test("comfort wording: backend alerts, out-of-range facts, or comfortable", () => {
  const settings = { comfort_temp_min_c: 20, comfort_temp_max_c: 26, comfort_rh_min: 30, comfort_rh_max: 60 };
  const inRange = { comfort_available: true, comfort_active: [],
                    temperature: { value: 23, quality: "valid" }, humidity: { value: 45, quality: "valid" } };
  assert.equal(L.comfortView(inRange, settings).text, "Comfortable");
  const warm = { ...inRange, temperature: { value: 27, quality: "valid" } };
  assert.equal(L.comfortView(warm, settings).text, "Outside your range · checking");
  assert.equal(L.comfortView({ ...warm, comfort_active: ["hot"] }, settings).text, "Warmer than your range");
  assert.equal(L.comfortView({ ...inRange, comfort_available: false }, settings).text, "Comfort check paused");
  // An invalid reading is not compared at all.
  const invalid = { ...inRange, temperature: { value: 40, quality: "invalid" } };
  assert.equal(L.comfortView(invalid, settings).text, "Comfortable");
});

test("proximity never claims a person", () => {
  for (const s of ["near", "clear", "unknown", null]) {
    assert.doesNotMatch(L.proximityView(s).text, /person|user|someone|present/i);
  }
  assert.equal(L.proximityView("near").text, "Object near");
  assert.equal(L.proximityView(undefined).text, "Unknown");
});

test("source badges come from the sample's own mode", () => {
  assert.equal(L.modeBadge("real").text, "Real");
  assert.equal(L.modeBadge("simulated").text, "Simulated");
  assert.equal(L.modeBadge(undefined), null);
});

test("health states map to icon plus text", () => {
  assert.equal(L.healthVisual("ready").text, "Ready");
  assert.equal(L.healthVisual("degraded").icon, "triangle-alert");
  assert.equal(L.healthVisual("unavailable").text, "Unavailable");
  assert.equal(L.isProblem({ state: "busy" }), false);
  assert.equal(L.isProblem({ state: "unavailable" }), true);
  assert.equal(L.componentName("speech"), "Bluetooth speaker and speech");
  assert.equal(L.componentName("new_thing"), "New thing");
  assert.match(L.healthHint({ state: "unavailable", reason: "mcu_unresponsive" }), /microcontroller/);
  assert.equal(L.healthHint({ state: "unavailable", reason: "some_raw_code" }), "Some raw code");
  assert.match(L.healthHint({ state: "unavailable", reason: "pixels_not_found" }), /Not detected on the I2C bus/);
  assert.match(L.healthHint({ state: "unavailable", reason: "light_disconnected" }), /Stopped answering/);
  assert.match(L.healthHint({ state: "unavailable", reason: "i2c_bus_stalled" }), /holding the sensor bus/);
});

const notice = {
  notice_id: "n1", category: "preparation", origin: "factual_fallback", lifecycle: "active",
  generation: { reason: "model_not_configured" },
  evidence_detail: [
    { evidence_id: "calendar.event.e", metric: "event", source_type: "calendar", label: "Fictional demo calendar",
      value: { title: "Design review", start: "2026-11-16T16:00:00.000Z", start_local: "10:00", location: "Room 204" },
      quality: "valid", current_quality: "valid", revision: 1, current_revision: 1, source_mode: "simulated" },
    { evidence_id: "weather.event", metric: "forecast_event", source_type: "weather", label: "Fictional demo forecast",
      value: { condition: "rainy", precipitation_probability: 70, temperature_c: 10 },
      quality: "valid", current_quality: "stale", revision: 1, current_revision: 1, source_mode: "simulated" },
    { evidence_id: "pref.temperature_range", metric: "preferred_temperature_range", source_type: "preference",
      label: "Your preference", value: [20, 26], unit: "degC", quality: "valid", current_quality: "valid", source_mode: "real" },
  ],
};

test("notice headline and countdown come from cited evidence", () => {
  assert.equal(L.noticeHeadline(notice), "Design review at 10:00 AM");
  assert.equal(L.noticeEventIn(notice, "2026-11-16T15:38:00.000Z"), 22 * 60000);
  assert.equal(L.noticeHeadline({ category: "comfort", evidence_detail: [] }), "Room comfort");
  // Board: "Your morning briefing" was shown at 9 PM.
  const briefing = { category: "morning", evidence_detail: [] };
  assert.equal(L.noticeHeadline(briefing, "07:45"), "Your morning briefing");
  assert.equal(L.noticeHeadline(briefing, "14:10"), "Your afternoon briefing");
  assert.equal(L.noticeHeadline(briefing, "21:05"), "Your evening briefing");
  assert.equal(L.noticeHeadline(briefing), "Your briefing");
  assert.equal(L.CATEGORY.morning, "Briefing");
  assert.equal(L.noticeEventIn({ category: "comfort", evidence_detail: [] }, "2026-11-16T15:38:00Z"), null);
});

test("provenance lists sources, not user preferences", () => {
  assert.deepEqual(L.provenance(notice), ["Fictional demo calendar", "Fictional demo forecast"]);
});

test("why-this lines are plain language with freshness", () => {
  const [ev, wx, pref] = notice.evidence_detail.map((e) => L.evidenceLine(e, "metric"));
  assert.equal(ev.text, "Design review at 10:00 AM, Room 204");
  assert.equal(wx.text, "Rainy, 70% chance of rain, 10.0 °C");
  assert.equal(wx.icon, "cloud-rain");
  assert.equal(pref.text, "20.0 °C to 26.0 °C");
  assert.equal(L.evidenceFreshness(notice.evidence_detail[0]).text, "Current");
  assert.equal(L.evidenceFreshness(notice.evidence_detail[1]).text, "Stale now");
  assert.equal(L.evidenceFreshness({ quality: "valid", current_quality: "no_longer_present" }).text, "No longer current");
});

test("later-today forecast and unknown evidence read as sentences, never JSON", () => {
  const later = { evidence_id: "weather.later", metric: "forecast_later", label: "Open-Meteo forecast",
                  value: { condition: "sunny", precipitation_probability: 35.0, temperature_c: 12, night: true,
                           start_local: "21:00", end_local: "00:00", period_id: "om-1" },
                  quality: "valid", source_mode: "real" };
  const line = L.evidenceLine(later, "metric");
  assert.equal(line.what, "Later today");
  assert.equal(line.text, "Clear, 35% chance of rain, 12.0 °C (9:00 PM–12:00 AM)");
  assert.equal(line.icon, "moon");
  const odd = L.evidenceLine({ metric: "new_thing", value: { a_b: 1, nested: { x: 1 } } }, "metric");
  assert.doesNotMatch(odd.text, /[{}"]/);
  assert.equal(odd.what, "New thing");
});

test("origin sentence never presents fallback text as AI output", () => {
  assert.match(L.originSentence(notice), /rules/);
  assert.match(L.originSentence(notice), /not set up/);
  assert.doesNotMatch(L.originSentence(notice), /AI model \(/);
  assert.match(L.originSentence({ origin: "generated", generation: { model: "qwen", execution_location: "uno_q_linux" } }), /AI model \(qwen\) on uno q linux/);
});

test("blocked play reasons are readable", () => {
  assert.equal(L.blockedText("audio_policy_visual_only"), "Audio is set to visual only");
  assert.equal(L.blockedText("odd_reason"), "Odd reason");
});

test("other notices exclude the selected and finished ones", () => {
  const list = [{ notice_id: "a", lifecycle: "active" }, { notice_id: "b", lifecycle: "deferred" },
                { notice_id: "c", lifecycle: "acknowledged" }, { notice_id: "d", lifecycle: "expired" }];
  assert.deepEqual(L.otherNotices(list, "a").map((n) => n.notice_id), ["b"]);
});
