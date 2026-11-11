/* ContextCue UI presentation logic: pure functions with no DOM access, so they
   can be unit-tested with Node (tests/ui/test_ui_logic.js). Every function maps
   structured backend state to text/icons; nothing here invents data. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CCLogic = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const humanize = (v) => String(v ?? "").replace(/_/g, " ");
  const cap = (v) => { const s = humanize(v); return s ? s[0].toUpperCase() + s.slice(1) : s; };

  /* ---------------------------------------------------------------- time */
  // "14:05" -> "2:05 PM". Returns the input unchanged if it is not HH:MM.
  function fmt12(hhmm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(String(hhmm ?? ""));
    if (!m) return hhmm ?? "";
    const h = Number(m[1]);
    return `${((h + 11) % 12) + 1}:${m[2]} ${h < 12 ? "AM" : "PM"}`;
  }

  function greeting(hhmm) {
    const h = Number(String(hhmm ?? "").split(":")[0]);
    if (!Number.isFinite(h)) return "Hello";
    if (h >= 5 && h < 12) return "Good morning";
    if (h >= 12 && h < 17) return "Good afternoon";
    return "Good evening";
  }

  // Sky palette for the header scene; "night" also covers quiet late hours.
  function skyPhase(hhmm) {
    const h = Number(String(hhmm ?? "").split(":")[0]);
    if (!Number.isFinite(h)) return "day";
    if (h >= 5 && h < 8) return "dawn";
    if (h >= 8 && h < 17) return "day";
    if (h >= 17 && h < 20) return "dusk";
    return "night";
  }

  function duration(ms) {
    const m = Math.max(0, Math.round(ms / 60000));
    if (m < 60) return `${m} min`;
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `${h} h ${r} min` : `${h} h`;
  }

  // Relative time until a start; null when unknown.
  function until(ms) {
    if (ms == null || !Number.isFinite(ms)) return null;
    if (ms <= 0) return "Now";
    // Round up, like "Expires in N min": 12.5 minutes away reads "In 13 min".
    const m = Math.ceil(ms / 60000);
    if (m < 60) return `In ${m} min`;
    const h = Math.floor(m / 60), r = m % 60;
    return r ? `In ${h} h ${r} m` : `In ${h} h`;
  }

  /* ------------------------------------------------------------- weather */
  // Deterministic map from WeatherPeriod.condition (contracts.py). Nothing
  // else may choose a weather icon, and unknown data never gets a sun.
  const WEATHER = {
    sunny: { icon: "sun", text: "Sunny" },
    cloudy: { icon: "cloud", text: "Cloudy" },
    rainy: { icon: "cloud-rain", text: "Rainy" },
    snowy: { icon: "cloud-snow", text: "Snowy" },
    foggy: { icon: "cloud-fog", text: "Foggy" },
  };

  // `night` (the period is after dark, local time) turns "sunny" into a clear
  // night: Open-Meteo's clear-sky code maps to sunny around the clock.
  function weatherVisual(forecast, night = false) {
    if (!forecast) return { icon: "cloud-off", text: "No forecast source", known: false };
    if (forecast.quality === "stale") return { icon: "cloud-off", text: "Forecast out of date", known: false };
    if (forecast.quality !== "valid") return { icon: "cloud-off", text: "Forecast unavailable", known: false };
    const hit = WEATHER[forecast.condition];
    if (!hit) return { icon: "circle-help", text: "Unknown conditions", known: false };
    if (night && forecast.condition === "sunny") return { icon: "moon", text: "Clear", known: true };
    return { ...hit, known: true };
  }

  // Night for weather wording: a local hour from 18:00 to 05:59.
  function isNightHour(hour) { return Number.isFinite(hour) && (hour >= 18 || hour < 6); }

  /* ------------------------------------------------------------- sensors */
  function fmtTemp(c, units) {
    if (c == null || !Number.isFinite(Number(c))) return null;
    return units === "imperial" ? `${(c * 9 / 5 + 32).toFixed(1)} °F` : `${Number(c).toFixed(1)} °C`;
  }

  const SAMPLE_FORMAT = {
    temperature: (v, units) => fmtTemp(v, units),
    humidity: (v) => `${Number(v).toFixed(0)} %`,
    ambient_light: (v) => `${Number(v).toFixed(0)} lux`,
    distance: (v) => `${Number(v).toFixed(0)} mm`,
  };

  // A reading is shown only when valid; otherwise its status is shown in
  // words, never a fabricated zero.
  function sampleView(metric, sample, units) {
    if (!sample) return { ok: false, text: "No data yet", tone: "muted" };
    if (sample.quality === "valid" && sample.value != null) {
      return { ok: true, text: SAMPLE_FORMAT[metric](sample.value, units), tone: "ok" };
    }
    if (sample.quality === "stale") return { ok: false, text: "Stale", tone: "warn", reason: sample.error };
    if (sample.quality === "disabled") return { ok: false, text: "Disabled", tone: "muted", reason: sample.error };
    return { ok: false, text: "Unavailable", tone: "bad", reason: sample.error };
  }

  const COMFORT = { hot: "Warmer than your range", cold: "Cooler than your range",
                    humid: "More humid than your range", dry: "Drier than your range" };

  // Alerts come only from the backend's sustained-condition rule. Without an
  // alert, a valid reading outside the configured range is reported as a fact
  // ("checking"), never as "Comfortable".
  function comfortView(indoor, settings) {
    if (!indoor || !indoor.comfort_available) return { text: "Comfort check paused", tone: "muted" };
    const active = indoor.comfort_active || [];
    if (active.length) return { text: active.map((k) => COMFORT[k] || cap(k)).join(" · "), tone: "warn" };
    if (settings) {
      const outside = (sample, lo, hi) => sample && sample.quality === "valid" && sample.value != null
        && (sample.value < lo || sample.value > hi);
      if (outside(indoor.temperature, settings.comfort_temp_min_c, settings.comfort_temp_max_c)
          || outside(indoor.humidity, settings.comfort_rh_min, settings.comfort_rh_max)) {
        return { text: "Outside your range · checking", tone: "muted" };
      }
    }
    return { text: "Comfortable", tone: "ok" };
  }

  // Distance measures objects only: never "person detected".
  function proximityView(state) {
    return { near: { text: "Object near", tone: "ok", icon: "circle-check" },
             clear: { text: "Clear", tone: "muted", icon: "radar" } }[state]
      || { text: "Unknown", tone: "warn", icon: "circle-help" };
  }

  function modeBadge(mode) {
    if (mode === "simulated") return { cls: "sim", text: "Simulated", icon: "flask-conical" };
    if (mode === "real") return { cls: "real", text: "Real", icon: "cpu" };
    return null;
  }

  /* -------------------------------------------------------------- health */
  const COMPONENT = {
    bridge: "Arduino Bridge", thermo: "Thermo sensor", light: "Light sensor",
    distance: "Distance sensor", pixels: "Pixels", buzzer: "Buzzer", output_lease: "Output safety lease",
    calendar_source: "Calendar", weather_source: "Weather forecast", model: "AI model",
    speech: "Bluetooth speaker and speech", storage: "Storage",
  };

  function componentName(id) { return COMPONENT[id] || cap(id); }

  function healthVisual(state) {
    if (state === "ready") return { icon: "circle-check", tone: "ok", text: "Ready" };
    if (state === "busy") return { icon: "activity", tone: "info", text: "Busy" };
    if (state === "degraded" || state === "stale") return { icon: "triangle-alert", tone: "warn", text: "Degraded" };
    if (state === "disabled") return { icon: "circle-help", tone: "muted", text: "Disabled" };
    return { icon: "circle-alert", tone: "bad", text: state ? cap(state) : "Unknown" };
  }

  const HINTS = {
    mcu_unresponsive: "The board's microcontroller is not answering. Resetting the board usually helps.",
    router_unavailable: "The Arduino Router service is not running on the board.",
    gateway_stopped: "The hardware connection was stopped.",
    no_tts_engine_selected: "No speech engine is installed yet, so notices are visual only.",
    not_detected: "The module did not answer on the I2C bus. Check its cable.",
    distance_timeout: "Nothing in range of the Distance sensor.",
    no_sample_yet: "Waiting for the first reading.",
    no_location_set: "Choose a forecast location in Settings, Calendar & Weather.",
    fetching: "Getting the latest data…",
    calendar_not_connected: "Connect your calendar's subscription link in Settings, Calendar & Weather.",
    not_a_calendar: "The link did not return a calendar. Check that it is the ICS (iCal) link.",
    calendar_too_large: "The calendar is too large to download (over 5 MB).",
    http_401: "The calendar link was refused (401). It may have been reset; paste the new one.",
    http_403: "The calendar link was refused (403). It may have been reset; paste the new one.",
    http_404: "The calendar link no longer exists (404). Paste the current link.",
    malformed_forecast: "The weather service sent an unexpected reply; retrying.",
    distance_bus_error: "The Distance sensor stopped answering mid-read. Retrying automatically.",
    i2c_bus_stalled: "Something is holding the sensor bus. Readings pause and resume on their own.",
  };

  // Friendly line for Device Status; the raw reason stays available in details.
  function healthHint(item) {
    if (!item || !item.reason) return item && item.state === "ready" ? "Working normally" : "";
    if (HINTS[item.reason]) return HINTS[item.reason];
    // Gateway module codes: thermo_not_found, light_disconnected, pixels_not_found ...
    if (/_not_found$/.test(item.reason)) return "Not detected on the I2C bus. Check its cable and connector.";
    if (/_disconnected$/.test(item.reason)) return "Stopped answering on the I2C bus. Check its cable.";
    if (/^network_error/.test(item.reason)) return "Could not reach the service; showing the last data while it is still fresh.";
    if (/^parse_error/.test(item.reason)) return "The calendar could not be read; retrying.";
    if (/^http_\d+$/.test(item.reason)) return `The calendar service answered with an error (${item.reason.slice(5)}); retrying.`;
    return cap(item.reason);
  }

  function isProblem(item) { return !["ready", "busy"].includes(item.state); }

  /* ------------------------------------------------------------- notices */
  const CATEGORY = { morning: "Morning briefing", preparation: "Get ready", comfort: "Room comfort",
                     context_answer: "Answer" };
  const CATEGORY_ICON = { morning: "sunrise", preparation: "calendar-clock", comfort: "thermometer",
                          context_answer: "lightbulb" };
  const ORIGIN = { generated: "AI generated", cached: "Saved reply", factual_fallback: "Factual fallback",
                   status: "Status" };

  function eventEvidence(notice) {
    return (notice.evidence_detail || []).find((e) => e.metric === "event" && e.value && e.value.title);
  }

  // Headline built only from evidence fields; otherwise a category label.
  function noticeHeadline(notice) {
    const ev = eventEvidence(notice);
    if (ev && notice.category === "preparation") return `${ev.value.title} at ${fmt12(ev.value.start_local)}`;
    if (notice.category === "morning") return "Your morning briefing";
    if (notice.category === "comfort") return "Room comfort";
    if (notice.category === "context_answer") return "Your answer";
    return CATEGORY[notice.category] || cap(notice.category);
  }

  // Milliseconds until the cited event starts, from the backend clock.
  function noticeEventIn(notice, utcNow) {
    const ev = eventEvidence(notice);
    if (!ev || !ev.value.start || !utcNow) return null;
    return Date.parse(ev.value.start) - Date.parse(utcNow);
  }

  // Unique source labels ("Fictional demo calendar · Fictional demo forecast").
  function provenance(notice) {
    const seen = [];
    for (const e of notice.evidence_detail || []) {
      if (e.source_type === "preference") continue;
      const label = e.label || e.provenance;
      if (label && !seen.includes(label)) seen.push(label);
    }
    return seen;
  }

  const METRIC = { temperature: "Room temperature", humidity: "Room humidity",
                   ambient_light: "Ambient light", distance: "Distance" };

  // One plain-language line per evidence item for "Why this?".
  function evidenceLine(e, units) {
    const v = e.value;
    const where = e.label || e.provenance || e.source_id;
    if (e.metric === "event" && v) {
      return { icon: "calendar", what: "Calendar", text: `${v.title} at ${fmt12(v.start_local)}${v.location ? `, ${v.location}` : ""}`, where };
    }
    if ((e.metric === "forecast_event" || e.metric === "forecast_next") && v) {
      const w = WEATHER[v.condition];
      const parts = [w ? w.text : "Unknown conditions"];
      if (v.precipitation_probability != null) parts.push(`${v.precipitation_probability}% chance of rain`);
      const t = fmtTemp(v.temperature_c, units);
      if (t) parts.push(t);
      return { icon: w ? w.icon : "circle-help", what: e.metric === "forecast_event" ? "Forecast for the event" : "Forecast", text: parts.join(", "), where };
    }
    if (e.metric === "schedule_today" && v) {
      return { icon: "calendar", what: "Today", text: `${v.event_count} events, ${duration(v.busy_ms)} scheduled`, where };
    }
    if (METRIC[e.metric]) {
      const s = sampleView(e.metric, { value: v, quality: e.quality }, units);
      return { icon: { temperature: "thermometer", humidity: "droplet", ambient_light: "sun-dim", distance: "scan" }[e.metric],
               what: METRIC[e.metric], text: s.text, where: e.source_mode === "real" ? "Measured by the board's sensor" : "Simulated sensor" };
    }
    if (String(e.metric).startsWith("comfort_")) {
      return { icon: "thermometer", what: "Comfort rule", text: `${cap(v)} for at least 60 s`, where: "ContextCue comfort rule" };
    }
    if (String(e.metric).startsWith("preferred_") && Array.isArray(v)) {
      const fmt = e.unit === "degC" ? (x) => fmtTemp(x, units) : (x) => `${x} %`;
      return { icon: "sliders-horizontal", what: "Your comfort range", text: `${fmt(v[0])} to ${fmt(v[1])}`, where: "Your settings" };
    }
    return { icon: "info", what: e.label || cap(e.metric), text: v == null ? "No value" : (typeof v === "object" ? JSON.stringify(v) : `${v}${e.unit ? " " + e.unit : ""}`), where };
  }

  // Freshness of an evidence item when used and now.
  function evidenceFreshness(e) {
    if (e.current_quality === "no_longer_present") return { text: "No longer current", tone: "warn" };
    if (e.quality !== "valid") return { text: `${cap(e.quality)} when used`, tone: "warn" };
    if (e.current_quality && e.current_quality !== "valid") return { text: `${cap(e.current_quality)} now`, tone: "warn" };
    if (e.current_revision != null && e.current_revision !== e.revision) return { text: "Updated since", tone: "info" };
    return { text: "Current", tone: "ok" };
  }

  // How the notice text was produced, in plain words.
  function originSentence(notice) {
    const g = notice.generation || {};
    if (notice.origin === "generated") {
      const where = g.execution_location && g.execution_location !== "none" ? ` on ${humanize(g.execution_location)}` : "";
      return `Written by the AI model${g.model ? ` (${g.model})` : ""}${where}. Its numbers were checked against the facts above.`;
    }
    if (notice.origin === "factual_fallback") {
      return `Written by ContextCue's rules from the facts above, because the AI model ${g.reason === "model_not_configured" ? "is not set up" : "did not give an accepted reply"}${g.reason && g.reason !== "model_not_configured" ? ` (${humanize(g.reason)})` : ""}.`;
    }
    if (notice.origin === "cached") return "A saved reply built from the same facts.";
    return "A status message from ContextCue.";
  }

  const BLOCKED = {
    audio_policy_visual_only: "Audio is set to visual only",
    muted: "Sound is muted", focus: "Focus is on", quiet_hours: "Quiet hours are on",
    expired: "This notice has expired", no_tts_engine_selected: "No speech engine is installed yet",
    already_announced: "Already announced", buzzer_not_enabled: "The Buzzer chime is off",
    buzzer_unavailable: "The Buzzer is unavailable", away_endpoint_disconnected_queued: "Speaker disconnected; queued",
    long_idle_clear_distance: "Nobody near the desk for a while",
  };
  function blockedText(reason) { return BLOCKED[reason] || cap(reason || "Not available"); }

  // Non-terminal notices other than the selected one.
  function otherNotices(notices, selectedId) {
    return (notices || []).filter((n) => n.notice_id !== selectedId && !["acknowledged", "expired"].includes(n.lifecycle));
  }

  return {
    humanize, cap, fmt12, greeting, skyPhase, duration, until, WEATHER, weatherVisual, isNightHour, fmtTemp,
    sampleView, comfortView, proximityView, modeBadge, componentName, healthVisual, healthHint,
    isProblem, CATEGORY, CATEGORY_ICON, ORIGIN, noticeHeadline, noticeEventIn, provenance,
    evidenceLine, evidenceFreshness, originSentence, blockedText, otherNotices,
  };
});
