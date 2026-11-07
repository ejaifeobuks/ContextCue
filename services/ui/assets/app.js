/* ContextCue kiosk UI. Holds only view state (current view, selected notice);
   every decision comes from the application service (UML class 1). */
"use strict";

const POLL_MS = 500;
const state = { data: null, view: "glance", whyNotice: null, rendered: {}, dragging: false, lastTouch: 0 };

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const words = (v) => esc(String(v ?? "").replace(/_/g, " "));

/* ------------------------------------------------------------ server */
async function poll() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    state.data = await res.json();
    $("#offline").hidden = true;
    render();
  } catch (e) {
    $("#offline").hidden = false;
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

async function act(action, payload = {}, quiet = false) {
  try {
    const res = await fetch("/api/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, payload }),
    });
    const body = await res.json();
    if (!res.ok) { toast(body.error || "Action failed", true); return null; }
    if (!quiet) toast(describe(action, body));
    state.rendered = {};
    return body;
  } catch (e) {
    toast("Could not reach the ContextCue app", true);
    return null;
  }
}

function describe(action, body) {
  if (action.startsWith("sim_")) return action === "sim_scenario" ? "Scenario loaded" : action === "sim_advance" ? `Scenario time +${body.seconds / 60} min` : "Simulation updated";
  if (body.status === "blocked") return `Not played: ${String(body.reason || "").replace(/_/g, " ")}`;
  if (action === "volume") return `Volume saved (${body.volume}%). Not applied: no audio adapter yet.`;
  return String(body.status || "Done").replace(/_/g, " ");
}

let toastTimer;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), 2600);
}

/* ----------------------------------------------------------- helpers */
function modeTag(mode) {
  if (mode === "simulated") return '<span class="tag sim">Simulated</span>';
  if (mode === "real") return '<span class="tag real">Real</span>';
  return "";
}
function qualityTag(q, error) {
  if (!q || q === "valid") return "";
  const cls = q === "stale" ? "stale" : "bad";
  return `<span class="tag ${cls}">${words(q)}</span>` + (error ? ` <span class="tag">${words(error)}</span>` : "");
}
function fmtTemp(c) {
  if (c == null) return null;
  return state.data.settings.units === "imperial" ? `${(c * 9 / 5 + 32).toFixed(1)} °F` : `${c.toFixed(1)} °C`;
}
function localTime(isoText) {
  if (!isoText) return "—";
  try {
    return new Date(isoText).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZone: state.data.settings.time_zone });
  } catch (e) { return isoText; }
}
function duration(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
const CATEGORY = { morning: "Morning briefing", preparation: "Get ready", comfort: "Comfort", context_answer: "Answer" };
const ORIGIN = { generated: "Generated", cached: "Cached", factual_fallback: "Factual fallback", status: "Status" };
const PRIORITY = { 2: "P2 urgent", 1: "P1", 0: "P0" };

/* ------------------------------------------------------------ render */
function render() {
  const d = state.data;
  if (!d) return;
  renderTop(d);
  const dim = $("#dim");
  dim.style.opacity = String(Math.max(0, (1 - d.display.screen_level) * 0.75).toFixed(2));
  const fn = { glance: renderGlance, briefing: renderBriefing, why: renderWhy, settings: renderSettings,
               audio: renderAudio, health: renderHealth, simulation: renderSimulation }[state.view];
  fn(d);
}

function renderTop(d) {
  $("#time").textContent = d.clock.local_time;
  $("#date").textContent = d.clock.local_date;
  const label = $("#clock-label");
  label.textContent = d.clock.label;
  label.className = "tag " + (d.clock.mode === "simulated" ? "sim" : "real");
  const focus = $("#chip-focus");
  focus.textContent = d.status.focus ? "Focus on" : "Normal";
  focus.classList.toggle("on", d.status.focus);
  focus.setAttribute("aria-pressed", d.status.focus);
  const mute = $("#chip-mute");
  mute.textContent = d.status.muted ? "Muted" : "Sound on";
  mute.classList.toggle("warn", d.status.muted);
  mute.setAttribute("aria-pressed", d.status.muted);
  const audio = $("#chip-audio");
  const policy = { visual_only: "Visual only", on_request: "Audio on request", automatic: "Audio automatic" }[d.status.audio_policy];
  audio.textContent = (d.status.quiet_hours ? "Quiet hours · " : "") + policy;
  const bad = d.health.filter((h) => !["ready", "busy"].includes(h.state));
  const chip = $("#chip-health");
  chip.textContent = bad.length ? `${bad.length} issue${bad.length > 1 ? "s" : ""}` : "All systems ready";
  chip.className = "chip " + (bad.length ? "warn" : "");
}

function noticeCard(n, compact = false) {
  const tags = [
    `<span class="tag info">${esc(CATEGORY[n.category] || n.category)}</span>`,
    `<span class="tag">${esc(PRIORITY[n.priority])}</span>`,
    `<span class="tag ${n.origin === "generated" ? "real" : "stale"}">${esc(ORIGIN[n.origin] || n.origin)}</span>`,
    modeTag(n.source_mode),
  ].join(" ");
  const quiet = state.data.status.focus || state.data.status.muted;
  return `<div class="notice ${n.priority >= 2 ? "p2" : ""} ${quiet ? "quiet" : ""}">
    <div>
      <div class="meta">${tags}<span class="tag">Expires in ${Math.ceil(n.expires_in_s / 60)} min</span></div>
      <div class="text">${esc(n.text || "Preparing…")}</div>
    </div>
    <div class="actions">
      <button class="btn" data-action="why" data-id="${esc(n.notice_id)}">Why</button>
      <button class="btn" data-action="play" data-id="${esc(n.notice_id)}" ${n.can_play ? "" : `title="${words(n.play_blocked_reason)}"`}>Play</button>
      <button class="btn primary" data-action="ack" data-id="${esc(n.notice_id)}">Done</button>
    </div>
  </div>`;
}

function renderGlance(d) {
  $("#notice-slot").innerHTML = d.notice
    ? noticeCard(d.notice)
    : `<div class="notice-empty">No active notice. ${d.status.focus ? "Focus is on: unsolicited briefings wait until you turn it off." : ""}</div>`;

  // Calendar
  const s = d.schedule;
  $("#sched-tag").outerHTML = `<span id="sched-tag">${s ? modeTag(s.source_mode) : ""}</span>`;
  let html = "";
  if (!s) html = '<div class="empty">No calendar source</div>';
  else if (s.quality !== "valid") html = `<div class="empty">Calendar ${words(s.quality)}</div>${qualityTag(s.quality, s.error)}`;
  else {
    const n = s.next_event;
    if (n) {
      const cls = s.urgency === "imminent" ? "imminent" : s.urgency === "soon" ? "soon" : "";
      html += `<div class="line">Next</div><div class="big-line">${esc(n.title)}</div>
        <div class="countdown ${cls}">${esc(n.start_local)}–${esc(n.end_local)} · in ${duration(s.next_event_in_ms)}</div>
        ${n.location ? `<div class="line">${esc(n.location)}</div>` : ""}`;
    } else html += '<div class="big-line">No more events today</div>';
    if (s.current_events && s.current_events.length) html += `<div class="line">Now: <strong>${s.current_events.map((e) => esc(e.title)).join(", ")}</strong></div>`;
    html += `<div class="line"><strong>${s.event_count}</strong> events today · <strong>${duration(s.busy_ms)}</strong> scheduled</div>`;
    if (s.all_day.length) html += `<div class="line">All day: ${s.all_day.map(esc).join(", ")}</div>`;
    html += `<div class="line" style="font-size:12px">${esc(s.label)}</div>`;
  }
  $("#sched-body").innerHTML = html;

  // Indoor
  const i = d.indoor;
  const tile = (label, sample, fmt) => {
    const ok = sample && sample.quality === "valid" && sample.value != null;
    const q = sample ? sample.quality : "no data";
    const value = ok ? `<div class="value">${fmt(sample.value)}</div>`
      : `<div class="value na"><span class="tag ${q === "stale" ? "stale" : "bad"}">${words(q)}</span></div>`;
    const why = !ok && sample && sample.error ? `<div class="err">${words(sample.error)}</div>` : "";
    return `<div class="reading"><div class="label">${label}</div>${value}${why}<div class="state">${sample ? modeTag(sample.source_mode) : ""}</div></div>`;
  };
  const proximity = { near: "Object near", clear: "Clear", unknown: "Unknown" }[i.proximity] || i.proximity;
  $("#indoor-body").innerHTML =
    tile("Temperature", i.temperature, fmtTemp) +
    tile("Humidity", i.humidity, (v) => `${v.toFixed(0)} %RH`) +
    tile("Light", i.ambient_light, (v) => `${v.toFixed(0)} lux`) +
    tile("Distance", i.distance, (v) => `${(v / 10).toFixed(0)} cm`) +
    `<div class="comfort-line">Proximity: <strong>${esc(proximity)}</strong> · ${
      !i.comfort_available ? "Comfort check paused (sensor unavailable)" :
      i.comfort_active.length ? "Outside your comfort range: " + i.comfort_active.map(words).join(", ") : "Within your comfort range"}</div>`;

  // Forecast
  const w = d.forecast;
  $("#wx-tag").outerHTML = `<span id="wx-tag">${w ? modeTag(w.source_mode) : ""}</span>`;
  if (!w) $("#wx-body").innerHTML = '<div class="empty">No forecast source</div>';
  else if (w.quality !== "valid" || !w.condition) $("#wx-body").innerHTML = `<div class="empty">Forecast ${words(w.quality)}</div>${qualityTag(w.quality, w.error)}<div class="line">Indoor readings never replace the forecast.</div>`;
  else $("#wx-body").innerHTML = `<div class="line">Forecast · ${esc(w.location)}</div>
    <div class="big-line">${esc(w.condition[0].toUpperCase() + w.condition.slice(1))}</div>
    <div class="line">Rain chance <strong>${w.precipitation_probability ?? "—"}%</strong></div>
    <div class="line">Outdoor <strong>${fmtTemp(w.temperature_c) ?? "—"}</strong></div>
    <div class="line">${localTime(w.start).slice(0, 5)}–${localTime(w.end).slice(0, 5)} period</div>
    <div class="line" style="font-size:12px">${esc(w.label)}</div>`;
}

function renderBriefing(d) {
  const key = JSON.stringify(d.questions);
  if (state.rendered.questions !== key) {
    $("#questions").innerHTML = Object.entries(d.questions)
      .map(([id, text]) => `<button class="btn" data-action="ask" data-id="${esc(id)}">${esc(text)}</button>`).join("");
    state.rendered.questions = key;
  }
  $("#notice-list").innerHTML = d.notices.length ? d.notices.map((n) => `
    <div class="row clickable" data-action="why" data-id="${esc(n.notice_id)}">
      <div class="head"><span>${esc(CATEGORY[n.category] || n.category)}</span>
        <span><span class="tag">${words(n.lifecycle)}</span> <span class="tag">${esc(ORIGIN[n.origin] || n.origin)}</span> ${modeTag(n.source_mode)}</span></div>
      <div>${esc(n.text || (n.lifecycle === "deferred" ? "Deferred by Focus" : "Preparing…"))}</div>
      <div class="detail">${n.ended_reason ? "Ended: " + words(n.ended_reason) + " · " : ""}Created ${localTime(n.created_at)}</div>
    </div>`).join("") : '<div class="empty">No notices yet.</div>';
}

function renderWhy(d) {
  const n = d.notices.find((x) => x.notice_id === state.whyNotice) || d.notice;
  if (!n) { $("#why-body").innerHTML = '<div class="notice-empty">Nothing to explain yet. Open Why from a notice.</div>'; return; }
  const g = n.generation || {};
  const fact = (k, v) => `<div class="fact"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const rows = (n.evidence_detail || []).map((e) => {
    let value = e.value;
    if (value && typeof value === "object") {
      value = e.metric === "schedule_today" ? `${value.event_count} events, ${duration(value.busy_ms)}` :
        e.metric === "event" ? `${value.title} at ${value.start_local}` :
        e.metric.startsWith("forecast") ? `${value.condition}, ${value.precipitation_probability}% rain` :
        Array.isArray(value) ? value.join("–") : JSON.stringify(value);
    }
    return `<tr><td><strong>${esc(e.label)}</strong><br><span class="result">${esc(e.evidence_id)} r${e.revision}</span></td>
      <td>${esc(value ?? "null")} ${esc(e.unit || "")}</td>
      <td>${esc(e.source_id)}</td>
      <td>${e.observed_at ? localTime(e.observed_at) : "—"}</td>
      <td>${e.valid_until ? localTime(e.valid_until) : "—"}</td>
      <td>${modeTag(e.source_mode)}</td>
      <td>${e.quality === "valid" ? "Valid" : words(e.quality)} at use<br>${e.current_quality === "valid" ? "Valid now" : words(e.current_quality) + " now"}</td></tr>`;
  }).join("");
  $("#why-body").innerHTML = `
    <div class="why-head"><div>
      <div class="meta">${modeTag(n.source_mode)} <span class="tag info">${esc(CATEGORY[n.category])}</span> <span class="tag">${words(n.lifecycle)}</span></div>
      <div class="why-text">${esc(n.text || "Preparing…")}</div></div>
      <button class="btn" data-view="glance">Back</button></div>
    <div class="facts">
      ${fact("Text origin", esc(ORIGIN[n.origin] || n.origin))}
      ${fact("Model request", words(n.request_outcome) + (g.reason ? ` (${words(g.reason)})` : ""))}
      ${fact("Ran on", words(g.execution_location || "—"))}
      ${fact("Validation", words(n.validation))}
      ${fact("Trigger", words(n.dedupe_key.split(":")[0]))}
      ${fact("Priority", esc(PRIORITY[n.priority]))}
      ${fact("Created", localTime(n.created_at))}
      ${fact("Expires", localTime(n.expires_at))}
    </div>
    <table><thead><tr><th>Input</th><th>Value</th><th>Source</th><th>Observed</th><th>Valid until</th><th>Mode</th><th>Freshness</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7">No evidence recorded.</td></tr>'}</tbody></table>
    <div class="note">Only the inputs above are evidence. ${n.origin === "factual_fallback" ? "This text was built by code from those inputs because generation did not produce an accepted result; it is not model output." : n.origin === "generated" ? "The model's wording is not evidence; numbers in it were checked against these inputs." : ""} Times shown in ${esc(d.settings.time_zone)}. ${d.clock.mode === "simulated" ? "Calendar and forecast times follow the scenario clock; sensor times stay on the device clock." : ""}</div>`;
}

/* -------------------------------------------------- settings widgets */
function seg(name, value, options) {
  return `<div class="seg">${options.map(([v, label]) => `<button class="${String(v) === String(value) ? "on" : ""}" data-set="${name}" data-value='${esc(JSON.stringify(v))}'>${esc(label)}</button>`).join("")}</div>`;
}
function toggle(name, value) {
  return `<button class="toggle ${value ? "on" : ""}" data-set="${name}" data-value='${JSON.stringify(!value)}'>${value ? "On" : "Off"}</button>`;
}
function stepper(name, value, step, fmt = (v) => v) {
  return `<span class="stepper"><button data-step="${name}" data-delta="${-step}">−</button><span class="val">${esc(fmt(value))}</span><button data-step="${name}" data-delta="${step}">+</button></span>`;
}
function timeStepper(name, value) {
  return `<span class="stepper"><button data-tstep="${name}" data-delta="-30">−</button><span class="val">${esc(value)}</span><button data-tstep="${name}" data-delta="30">+</button></span>`;
}
const field = (name, sub, control) => `<div class="field"><span class="name">${name}${sub ? `<small>${sub}</small>` : ""}</span>${control}</div>`;

function renderSettings(d) {
  const s = d.settings;
  const key = JSON.stringify(s);
  if (state.rendered.settings === key) return;
  state.rendered.settings = key;
  $("#settings-body").innerHTML = `
    <div class="group"><h3>Mode</h3>
      ${field("Focus", "Defers unsolicited briefings and automatic speech", `<button class="toggle ${s.focus ? "on" : ""}" data-action="toggle-focus">${s.focus ? "On" : "Off"}</button>`)}
      ${field("Speech during Focus", "Urgent preparation reminders only", toggle("focus_p2_speech", s.focus_p2_speech))}
      ${field("Morning window", "Briefing on approach, once per day", `${timeStepper("morning_start", s.morning_start)} ${timeStepper("morning_end", s.morning_end)}`)}
      ${field("Quiet hours", "No automatic speech or Buzzer", `${timeStepper("quiet_start", s.quiet_start)} ${timeStepper("quiet_end", s.quiet_end)}`)}
    </div>
    <div class="group"><h3>Comfort range</h3>
      ${field("Lowest temperature", "", stepper("comfort_temp_min_c", s.comfort_temp_min_c, 0.5, (v) => v.toFixed(1) + " °C"))}
      ${field("Highest temperature", "", stepper("comfort_temp_max_c", s.comfort_temp_max_c, 0.5, (v) => v.toFixed(1) + " °C"))}
      ${field("Lowest humidity", "", stepper("comfort_rh_min", s.comfort_rh_min, 5, (v) => v.toFixed(0) + " %"))}
      ${field("Highest humidity", "", stepper("comfort_rh_max", s.comfort_rh_max, 5, (v) => v.toFixed(0) + " %"))}
    </div>
    <div class="group"><h3>Display</h3>
      ${field("Units", "", seg("units", s.units, [["metric", "°C"], ["imperial", "°F"]]))}
      ${field("Time zone", "", seg("time_zone", s.time_zone, [["America/Chicago", "Central"], ["America/New_York", "Eastern"], ["America/Los_Angeles", "Pacific"], ["UTC", "UTC"]]))}
      ${field("Brightness", s.brightness_override == null ? "Automatic from the Light sensor (uncalibrated)" : "Manual", seg("brightness_override", s.brightness_override ?? "auto", [["auto", "Auto"], [0.4, "40%"], [0.7, "70%"], [1, "100%"]]))}
    </div>
    <div class="group"><h3>Sources</h3>
      ${field("Calendar", s.calendar_source === "fixture" ? "Fictional demo calendar" : "Live adapter not connected yet", seg("calendar_source", s.calendar_source, [["fixture", "Demo"], ["google", "Google"], ["microsoft365", "Microsoft 365"]]))}
      ${field("Weather", s.weather_source === "fixture" ? "Fictional demo forecast" : "Live adapter not connected yet", seg("weather_source", s.weather_source, [["fixture", "Demo"], ["open_meteo", "Open-Meteo"]]))}
      ${field("Stored data", "Notices, evidence and logs (settings kept)", `<button class="btn danger" data-action="clear-data">Clear</button>`)}
    </div>`;
}

function renderAudio(d) {
  const a = d.audio;
  const key = JSON.stringify([a, d.status.muted, d.status.playing]);
  if (state.rendered.audio === key || state.dragging) return;
  state.rendered.audio = key;
  const yes = (v) => (v ? "Yes" : "No");
  $("#audio-body").innerHTML = `
    <div class="group"><h3>Speech output</h3>
      ${field("Speech engine", "", `<span>${esc(a.engine || "None selected")}</span>`)}
      ${field("Speaker", "", `<span>${esc(a.endpoint || "None selected")}</span>`)}
      ${field("Paired", "", `<span>${yes(a.paired)}</span>`)}
      ${field("Connected", "", `<span>${yes(a.connected)}</span>`)}
      ${field("Ready to play", a.reason ? words(a.reason) : "", `<span>${yes(a.ready)}</span>`)}
      <p class="note">Playback finishing does not prove anyone heard it.</p>
    </div>
    <div class="group"><h3>Controls</h3>
      ${field("Mute", "Silences speech and Buzzer; notices stay on screen", `<button class="toggle ${d.status.muted ? "on" : ""}" data-action="toggle-mute">${d.status.muted ? "Muted" : "Off"}</button>`)}
      ${field("Speech volume", `<span id="vol-val">${a.volume}%</span>`, `<input id="volume" type="range" min="0" max="100" step="5" value="${a.volume}">`)}
      ${field("Audio policy", "", seg("audio_policy", a.policy, [["visual_only", "Visual only"], ["on_request", "On request"], ["automatic", "Automatic"]]))}
      ${field("Buzzer chime", "Separate opt-in, short tone only", toggle("buzzer_enabled", a.buzzer_enabled))}
      ${field("Away announcements", "Once, only to a connected speaker", toggle("away_audio_enabled", d.settings.away_audio_enabled))}
      ${field("Stop audio", d.status.playing ? "Playing now" : "Nothing playing", `<button class="btn" data-action="stop-audio">Stop</button>`)}
    </div>`;
}

function renderHealth(d) {
  $("#health-list").innerHTML = d.health.map((h) => `
    <div class="row"><div class="head"><span>${words(h.component_id)}</span>
      <span class="tag ${h.state === "ready" ? "real" : h.state === "degraded" || h.state === "busy" ? "stale" : "bad"}">${words(h.state)}</span></div>
      <div class="detail">${h.reason ? esc(h.reason) : "No problem reported"}${h.execution_location ? " · " + words(h.execution_location) : ""}</div></div>`).join("") +
    (d.startup_notes.length ? `<div class="row"><div class="head">Startup notes</div><div class="detail">${d.startup_notes.map(esc).join("<br>")}</div></div>` : "");
  $("#diag-list").innerHTML = d.diagnostics.map((x) => {
    const { at, kind, ...rest } = x;
    return `<div class="row"><div class="head"><span>${esc(kind)}</span><span>${localTime(at)}</span></div><div class="detail">${esc(JSON.stringify(rest))}</div></div>`;
  }).join("");
}

function renderSimulation(d) {
  const s = d.simulation;
  const key = JSON.stringify([s, d.gateway_mode]);
  if (state.rendered.sim === key || state.dragging) return;
  state.rendered.sim = key;
  const v = s.values;
  const f = s.faults;
  const slider = (metric, label, min, max, step, unit) => field(label, `<span id="sv-${metric}">${v[metric]} ${unit}</span>`,
    `<input type="range" data-sensor="${metric}" min="${min}" max="${max}" step="${step}" value="${v[metric]}">`);
  const fault = (target, label) => field(label, f[target] ? words(f[target]) : "Working", `<button class="toggle ${f[target] ? "on" : ""}" data-fault="${target}" data-active="${!f[target]}">${f[target] ? "Failed" : "OK"}</button>`);
  $("#sim-body").innerHTML = `
    <div class="group full"><h3>Scenario <span class="tag sim">Simulated time</span></h3>
      <p class="hint">${esc(s.scenario_description)}</p>
      ${seg("scenario", s.scenario_id, s.scenarios.map((x) => [x.scenario_id, x.title]))}
      ${s.time_controls ? `<div style="margin-top:10px;display:flex;gap:8px">
        <button class="btn" data-advance="60">+1 min</button><button class="btn" data-advance="600">+10 min</button>
        <button class="btn" data-advance="3600">+1 hour</button></div>` : ""}
    </div>
    ${s.sensor_controls ? `
    <div class="group"><h3>Simulated readings <span class="tag sim">Simulated</span></h3>
      ${slider("temperature", "Temperature", 10, 35, 0.5, "°C")}
      ${slider("humidity", "Humidity", 10, 90, 1, "%RH")}
      ${slider("ambient_light", "Light", 0, 1000, 10, "lux")}
      ${slider("distance", "Distance", 100, 2000, 50, "mm")}
      <div style="display:flex;gap:8px;margin-top:8px"><button class="btn" data-sensor-set="distance" data-value="350">Approach</button>
      <button class="btn" data-sensor-set="distance" data-value="1200">Leave</button></div>
    </div>
    <div class="group"><h3>Faults</h3>
      ${fault("thermo", "Thermo")}${fault("light", "Light")}${fault("distance", "Distance")}
      ${fault("bridge", "Bridge")}${fault("calendar", "Calendar source")}${fault("weather", "Weather source")}
      ${field("MCU reboot", "New boot identity", `<button class="btn" data-fault="reboot" data-active="true">Reboot</button>`)}
    </div>` : `<div class="group full"><h3>Real hardware</h3><p class="hint">Sensor values come from the real gateway. Only fixture time can be simulated here; sensors are never faked on the hardware path.</p></div>`}`;
}

/* ------------------------------------------------------------ events */
function show(view) {
  state.view = view;
  state.rendered = {};
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === `view-${view}`));
  document.querySelectorAll("#nav button").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  render();
}

function setStep(name, delta) {
  const s = state.data.settings;
  act("settings", { changes: { [name]: Math.round((s[name] + delta) * 10) / 10 } });
}
function setTimeStep(name, delta) {
  const [h, m] = state.data.settings[name].split(":").map(Number);
  const total = (h * 60 + m + delta + 1440) % 1440;
  act("settings", { changes: { [name]: `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}` } });
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, .row.clickable");
  const now = Date.now();
  if (now - state.lastTouch > 5000) { state.lastTouch = now; act("touch", {}, true); }
  if (!t || !state.data) return;
  const d = state.data;
  if (t.dataset.view) return show(t.dataset.view);
  if (t.dataset.set) {
    let value = JSON.parse(t.dataset.value);
    if (t.dataset.set === "scenario") return act("sim_scenario", { scenario_id: value });
    if (t.dataset.set === "brightness_override" && value === "auto") value = null;
    return act("settings", { changes: { [t.dataset.set]: value } });
  }
  if (t.dataset.step) return setStep(t.dataset.step, Number(t.dataset.delta));
  if (t.dataset.tstep) return setTimeStep(t.dataset.tstep, Number(t.dataset.delta));
  if (t.dataset.advance) return act("sim_advance", { seconds: Number(t.dataset.advance) });
  if (t.dataset.fault) return act("sim_fault", { target: t.dataset.fault, active: t.dataset.active === "true" });
  if (t.dataset.sensorSet) return act("sim_sensor", { metric: t.dataset.sensorSet, value: Number(t.dataset.value) });
  switch (t.dataset.action) {
    case "toggle-focus": return act("focus", { enabled: !d.status.focus });
    case "toggle-mute": return act("mute", { muted: !d.status.muted });
    case "ack": return act("acknowledge", { notice_id: t.dataset.id });
    case "play": {
      const n = d.notices.find((x) => x.notice_id === t.dataset.id);
      if (n && n.play_blocked_reason === "audio_policy_visual_only") {
        if (confirm("Audio is set to visual only. Switch to audio on request?")) {
          await act("settings", { changes: { audio_policy: "on_request" } }, true);
          return act("play", { notice_id: t.dataset.id });
        }
        return;
      }
      return act("play", { notice_id: t.dataset.id });
    }
    case "why": state.whyNotice = t.dataset.id; return show("why");
    case "briefing": await act("briefing"); return show("glance");
    case "ask": await act("ask", { question: t.dataset.id }); return show("glance");
    case "stop-audio": return act("stop_audio");
    case "clear-data": if (confirm("Clear stored notices, evidence and logs? Settings are kept.")) return act("clear_data");
  }
});

document.addEventListener("pointerdown", (ev) => { if (ev.target.type === "range") state.dragging = true; });
document.addEventListener("pointerup", () => { state.dragging = false; });
document.addEventListener("input", (ev) => {
  if (ev.target.id === "volume") $("#vol-val").textContent = `${ev.target.value}%`;
  if (ev.target.dataset.sensor) {
    const units = { temperature: "°C", humidity: "%RH", ambient_light: "lux", distance: "mm" };
    $(`#sv-${ev.target.dataset.sensor}`).textContent = `${ev.target.value} ${units[ev.target.dataset.sensor]}`;
  }
});
document.addEventListener("change", (ev) => {
  state.dragging = false;
  if (ev.target.id === "volume") act("volume", { volume: Number(ev.target.value) });
  if (ev.target.dataset.sensor) act("sim_sensor", { metric: ev.target.dataset.sensor, value: Number(ev.target.value) }, true);
});

poll();
