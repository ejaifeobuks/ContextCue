/* ContextCue kiosk UI: two tabs (Glance, Settings). Holds only view state;
   every decision and value comes from the application service through
   GET /api/state and POST /api/action. Presentation rules live in ui-logic.js. */
"use strict";

const L = window.CCLogic;
const POLL_MS = 500;
const SECTIONS = [
  ["general", "General", "settings"],
  ["sources", "Calendar & Weather", "calendar"],
  ["sound", "Sound & Notifications", "volume-2"],
  ["focus", "Focus & Comfort", "moon"],
  ["status", "Device Status", "activity"],
  ["developer", "Developer & Demo", "code-xml"],
];

const state = {
  data: null, tab: "glance", section: "general",
  whyOpen: null, whyDetails: false, sheet: null, dialog: null,
  demo: loadDemo(), diagOpen: false, techOpen: false,
  // Forecast location search (Settings > Calendar & Weather).
  locQuery: "", locResults: null, locBusy: false, locError: "", locKeyboard: false, calBusy: false, calLink: "",
  dragging: false, lastTouch: 0, layout: null,
};

/* Demo Mode is display-only and kept in this browser, not in app settings. */
function loadDemo() { try { return localStorage.getItem("cc.demoMode") === "on"; } catch (e) { return false; } }
function saveDemo(on) { try { localStorage.setItem("cc.demoMode", on ? "on" : "off"); } catch (e) { /* private mode */ } }

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Icons point at symbols inlined once by loadIcons(), so re-rendered markup
// never re-resolves an external file (which can flash on a slow board).
const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#${name}"></use></svg>`;
const pill = (tone, text, ic) => `<span class="pill ${tone}" title="${esc(text)}">${ic ? icon(ic) : ""}<span>${esc(text)}</span></span>`;
const modePill = (mode) => { const b = L.modeBadge(mode); return b ? pill(b.cls, b.text, b.icon) : ""; };

// Replace markup only when it changed, so polling does not reset focus,
// pressed states or open controls.
function put(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}
// The UI polls twice a second. Writing an unchanged value still makes
// Chromium repaint (textContent replaces the text node), and on the UNO Q
// every header repaint redraws the sky under it: that kept the kiosk at about
// 1.4 CPU cores and halved the on-board model's speed. Write only changes.
function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
function setAttr(el, name, value) { if (el && el.getAttribute(name) !== value) el.setAttribute(name, value); }

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
    if (!quiet) toast(describe(action, body), body.status === "blocked");
    return body;
  } catch (e) {
    toast("Could not reach the ContextCue app", true);
    return null;
  }
}

function describe(action, body) {
  if (action === "sim_scenario") return "Scenario loaded";
  if (action === "sim_advance") return `Scenario time moved ${L.duration(body.seconds * 1000)} ahead`;
  if (action.startsWith("sim_")) return "Simulation updated";
  if (body.status === "blocked") return `Not played: ${L.blockedText(body.reason)}`;
  if (action === "volume") return body.applied ? `Volume ${body.volume}%` : `Volume saved (${body.volume}%). Applied once the voice is ready.`;
  return { acknowledged: "Marked as done", requested: "Request sent", muted: "Sound muted", unmuted: "Sound on",
           focus_on: "Focus on", focus_off: "Focus off", stopped: "Audio stopped", ok: "Saved" }[body.status]
    || L.cap(body.status || "Done");
}

let toastTimer;
function toast(text, error = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = "show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), 2800);
}

/* ----------------------------------------------------------- helpers */
function zone() { return state.data.settings.time_zone; }
function units() { return state.data.settings.units; }

function clockOf(iso, withSeconds = false) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}), timeZone: zone() });
  } catch (e) { return iso; }
}

function longDate(d) {
  try {
    return new Date(d.clock.utc).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", timeZone: d.settings.time_zone });
  } catch (e) { return d.clock.local_date; }
}

/* ------------------------------------------------------------ render */
function render() {
  const d = state.data;
  if (!d) return;
  // ?why=1 opens "Why this?" on whichever notice the backend selected.
  if (state.whyOpen === "__selected__") state.whyOpen = d.notice ? d.notice.notice_id : null;
  renderHeader(d);
  // Awake: up to 75% dark from the Light sensor. Asleep: nearly black, the
  // clock still faintly readable (the backlight itself stays on).
  const dim = $("#dim"), opacity = (d.display.awake ? Math.max(0, (1 - d.display.screen_level) * 0.75) : 0.92).toFixed(2);
  if (dim.style.opacity !== opacity) dim.style.opacity = opacity;
  if (state.tab === "glance") renderGlance(d);
  // Not while a link is being pasted: a re-render would empty the field.
  else if (!state.dragging && !(document.activeElement && document.activeElement.id === "cal-link")) renderSettings(d);
  renderSheet(d);
  renderDialog();
}

function renderHeader(d) {
  const frame = $("#frame");
  setAttr(frame, "data-sky", L.skyPhase(d.clock.local_time));
  const badges = [
    d.gateway_mode === "simulated" ? pill("sim", "Simulated sensors", "flask-conical") : pill("real", "Real sensors", "cpu"),
    state.demo ? pill("demo", "Demo Mode", "monitor") : "",
  ];
  put($("#badges"), badges.join(""));

  const focus = $("#btn-focus");
  put(focus, `${icon("moon")}<span>${d.status.focus ? "Focus on" : "Focus"}</span>`);
  focus.classList.toggle("on", d.status.focus);
  setAttr(focus, "aria-pressed", String(d.status.focus));

  const mute = $("#btn-mute");
  put(mute, `${icon(d.status.muted ? "volume-x" : "volume-2")}<span>${d.status.muted ? "Muted" : "Sound on"}</span>`);
  mute.classList.toggle("warn", d.status.muted);
  setAttr(mute, "aria-pressed", String(d.status.muted));

  const problems = d.health.filter(L.isProblem);
  const status = $("#btn-status");
  put(status, problems.length
    ? `${icon("triangle-alert")}<span>${problems.length} issue${problems.length > 1 ? "s" : ""}</span>`
    : `${icon("circle-check")}<span>All ready</span>`);
  setAttr(status, "class", "hbtn " + (problems.length ? "warn" : "ok"));
  setAttr(status, "aria-label", `Device status: ${problems.length ? problems.length + " issues" : "all ready"}`);

  setText($("#time"), L.fmt12(d.clock.local_time));
  setText($("#clock-label"), d.clock.mode === "simulated" ? "Scenario time" : "Device time");
}

/* ------------------------------------------------------------ glance */
function layout(demo) {
  if (state.layout === demo) return;
  state.layout = demo;
  const main = $("#col-main"), side = $("#col-side"), row = $("#row-cards");
  const [today, comfort, weather, sensors] = ["#card-today", "#card-comfort", "#card-weather", "#card-sensors"].map($);
  $("#glance-grid").classList.toggle("demo", demo);
  if (demo) {
    row.append(today, weather);
    side.append(sensors);
    $("#tab-glance > div[hidden]").append(comfort);
  } else {
    row.append(today);
    side.append(comfort, weather);
    $("#tab-glance > div[hidden]").append(sensors);
  }
  main.append(row);
}

function renderGlance(d) {
  setText($("#greeting"), L.greeting(d.clock.local_time));
  const fictional = [d.schedule, d.forecast].some((x) => x && x.source_mode === "simulated");
  put($("#dateline"), `<span>${esc(longDate(d))}</span>${d.clock.mode === "simulated" ? `<span class="dot">·</span><span>Scenario ${esc(L.fmt12(d.clock.local_time))}</span>` : ""}${fictional ? pill("sim on-sky", "Demo data", "flask-conical") : ""}`);
  layout(state.demo);
  const hero = $("#hero");
  const wasOpen = !!hero.querySelector(".why");
  put(hero, d.notice ? heroNotice(d.notice, d) : heroUpNext(d));
  setAttr(hero, "class", "hero card" + (d.notice && d.notice.priority >= 2 ? " urgent" : ""));
  // With "Why this?" open the hero takes the whole column and scrolls.
  const why = hero.querySelector(".why");
  $("#col-main").classList.toggle("why-open", !!why);
  if (why && !wasOpen) why.scrollIntoView({ block: "nearest" });
  put($("#card-today"), todayCard(d));
  put($("#card-weather"), weatherCard(d));
  if (state.demo) put($("#card-sensors"), sensorsCard(d));
  else put($("#card-comfort"), comfortCard(d));
}

function heroNotice(n, d) {
  const inMs = L.noticeEventIn(n, d.clock.utc);
  const rel = n.priority >= 2 ? pill("bad", "Urgent", "bell")
    : inMs != null ? pill(inMs <= 15 * 60000 ? "warn" : "info", L.until(inMs), "clock") : "";
  const open = state.whyOpen === n.notice_id;
  const quiet = d.status.focus ? "Focus on: no automatic sound" : d.status.muted ? "Muted: shown on screen only" : "";
  const others = L.otherNotices(d.notices, n.notice_id);
  const meta = [
    pill(n.origin === "generated" ? "info" : "muted", L.ORIGIN[n.origin] || L.cap(n.origin), n.origin === "generated" ? "sparkles" : "check"),
    ...L.provenance(n).map((p) => `<span>${esc(p)}</span>`),
    `<span>Expires in ${Math.ceil(n.expires_in_s / 60)} min</span>`,
    quiet ? `<span>${esc(quiet)}</span>` : "",
  ].filter(Boolean).join('<span class="dot">·</span>');
  return `
    <div class="hero-top">
      <span class="cat">${icon(L.CATEGORY_ICON[n.category] || "bell")}</span>
      <span class="cat-label">${esc(L.CATEGORY[n.category] || L.cap(n.category))}</span>
      ${modePill(n.source_mode)}
      <span class="grow"></span>${rel}
    </div>
    <h2 class="headline">${esc(L.noticeHeadline(n, d.clock.local_time))}</h2>
    <p class="body">${esc(n.text || (n.lifecycle === "generating" ? "Preparing…" : "No text yet"))}</p>
    <div class="meta">${meta}</div>
    <div class="actions">
      <button class="btn" data-action="why" data-id="${esc(n.notice_id)}" aria-expanded="${open}">${icon("info")}Why this?${icon(open ? "chevron-up" : "chevron-down", "trail")}</button>
      <button class="btn ${n.can_play ? "" : "soft"}" data-action="play" data-id="${esc(n.notice_id)}" ${n.can_play ? "" : `title="${esc(L.blockedText(n.play_blocked_reason))}"`}>${icon("volume-2")}Play</button>
      <button class="btn primary" data-action="ack" data-id="${esc(n.notice_id)}">${icon("check")}Done</button>
      ${others.length ? `<span class="grow"></span><button class="btn ghost" data-sheet="notices">${icon("bell")}${others.length} more</button>` : ""}
    </div>
    ${open ? whyPanel(n, d) : ""}`;
}

function whyPanel(n, d) {
  const items = (n.evidence_detail || []).map((e) => {
    const line = L.evidenceLine(e, units());
    const fresh = L.evidenceFreshness(e);
    return `<li>${icon(line.icon)}<div><div class="w-what">${esc(line.what)}: <strong>${esc(line.text)}</strong></div>
      <div class="w-src"><span>${esc(line.where)}</span>${e.source_type === "preference" ? "" : modePill(e.source_mode)}${pill(fresh.tone, fresh.text)}${e.observed_at ? `<span>observed ${esc(clockOf(e.observed_at))}</span>` : ""}</div></div></li>`;
  }).join("");
  return `
    <div class="why">
      ${icon("lightbulb", "why-ic")}
      <div class="why-main">
        <h3>Why you're seeing this</h3>
        <ul>${items || "<li>No supporting facts were recorded.</li>"}</ul>
        <p class="why-origin">${esc(L.originSentence(n))}</p>
        <button class="link" data-action="why-details" aria-expanded="${state.whyDetails}">${state.whyDetails ? "Hide" : "Show"} technical details</button>
        ${state.whyDetails ? techDetails(n, d) : ""}
      </div>
    </div>`;
}

function techDetails(n, d) {
  const g = n.generation || {};
  const fact = (k, v) => `<div class="fact"><div class="k">${k}</div><div class="v">${esc(v)}</div></div>`;
  const rows = (n.evidence_detail || []).map((e) => `<tr><td>${esc(e.evidence_id)} r${esc(e.revision)}</td><td>${esc(e.source_id)}</td>
    <td>${esc(e.observed_at ? clockOf(e.observed_at, true) : "—")}</td><td>${esc(e.valid_until ? clockOf(e.valid_until, true) : "—")}</td>
    <td>${esc(L.humanize(e.quality))} / ${esc(L.humanize(e.current_quality))}</td></tr>`).join("");
  return `<div class="tech">
    <div class="facts">
      ${fact("Notice", n.notice_id)}${fact("Trigger", L.humanize(String(n.dedupe_key).split(":")[0]))}
      ${fact("Priority", "P" + n.priority)}${fact("Lifecycle", L.humanize(n.lifecycle))}
      ${fact("Model request", L.humanize(n.request_outcome) + (g.reason ? ` (${L.humanize(g.reason)})` : ""))}
      ${fact("Validation", L.humanize(n.validation))}
      ${fact("Created", clockOf(n.created_at, true))}${fact("Expires", clockOf(n.expires_at, true))}
    </div>
    <table><thead><tr><th>Evidence</th><th>Source</th><th>Observed</th><th>Valid until</th><th>Quality used / now</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="note">Times in ${esc(d.settings.time_zone)}. ${d.clock.mode === "simulated" ? "Calendar and forecast times follow the scenario clock; sensor times use the device clock." : ""}</p>
  </div>`;
}

function emptyHero(ic, title, text) {
  return `<div class="hero-empty">${icon(ic, "big-ic")}<div><h2 class="headline">${esc(title)}</h2><p class="body">${esc(text)}</p></div></div>`;
}

function heroUpNext(d) {
  const s = d.schedule;
  const focusNote = d.status.focus ? `<p class="note">${icon("moon")} Focus is on: briefings wait until you turn it off.</p>` : "";
  if (!s) return emptyHero("calendar", "No calendar connected", "Choose a calendar in Settings → Calendar & Weather.") + focusNote;
  if (s.quality !== "valid") {
    return emptyHero("cloud-off", s.quality === "stale" ? "Calendar out of date" : "Calendar unavailable",
      s.error ? L.cap(s.error) : "ContextCue cannot read your calendar right now.") + focusNote;
  }
  const n = s.next_event;
  if (!n) {
    return emptyHero("calendar", "No more events today", s.all_day && s.all_day.length ? `All day: ${s.all_day.join(", ")}` : "Your calendar is clear for the rest of the day.") + focusNote;
  }
  const tone = s.urgency === "imminent" ? "bad" : s.urgency === "soon" ? "warn" : "info";
  return `
    <div class="hero-top">
      <span class="cat">${icon("calendar")}</span><span class="cat-label">Up next</span>
      ${modePill(s.source_mode)}<span class="grow"></span>${pill(tone, L.until(s.next_event_in_ms), "clock")}
    </div>
    <h2 class="headline">${esc(n.title)}</h2>
    <p class="body">${esc(L.fmt12(n.start_local))} – ${esc(L.fmt12(n.end_local))}${n.location ? ` · ${icon("map-pin", "inline")}${esc(n.location)}` : ""}</p>
    <div class="meta"><span>${s.event_count} event${s.event_count === 1 ? "" : "s"} today</span><span class="dot">·</span><span>${esc(s.label)}</span></div>
    ${focusNote}
    <p class="note">No notice right now. Tap <strong>Brief me</strong> for a summary, or <strong>Ask</strong> a question.</p>`;
}

function todayCard(d) {
  const s = d.schedule;
  const head = `<div class="card-h">${icon("calendar")}<h3>Today</h3>${s ? modePill(s.source_mode) : ""}</div>`;
  if (!s) return head + `<div class="state-msg">No calendar source</div>`;
  if (s.quality !== "valid") return head + `<div class="state-msg">${pill(s.quality === "stale" ? "warn" : "bad", s.quality === "stale" ? "Out of date" : "Unavailable")}</div>${s.error ? `<div class="meta">${esc(L.cap(s.error))}</div>` : ""}`;
  const current = new Set((s.current_events || []).map((e) => e.event_id));
  const list = [...(s.current_events || []), ...(s.remaining_events || []).filter((e) => !current.has(e.event_id))];
  const limit = state.demo ? 1 : 3;
  const rows = list.slice(0, limit).map((e) => `<li class="${current.has(e.event_id) ? "now" : ""}">
    <span class="t">${current.has(e.event_id) ? "Now" : esc(L.fmt12(e.start_local))}</span><span class="e">${esc(e.title)}</span></li>`).join("");
  const summary = `<span class="summary"><strong>${s.event_count}</strong> event${s.event_count === 1 ? "" : "s"} · ${esc(L.duration(s.busy_ms))}</span>`;
  if (state.demo) {
    return `<div class="card-h">${icon("calendar")}<h3>Today</h3>${modePill(s.source_mode)}</div>${summary}
      <ul class="events">${rows || "<li><span class=\"e\">Nothing else scheduled</span></li>"}</ul>`;
  }
  return `<div class="card-h">${icon("calendar")}<h3>Today</h3>${summary}${modePill(s.source_mode)}</div>
    <ul class="events">${rows || "<li><span class=\"e\">Nothing else scheduled</span></li>"}</ul>
    ${list.length > limit ? `<div class="meta">+${list.length - limit} more later today</div>` : ""}`;
}

function comfortCard(d) {
  const i = d.indoor;
  const t = L.sampleView("temperature", i.temperature, units());
  const h = L.sampleView("humidity", i.humidity, units());
  const c = L.comfortView(i, d.settings);
  return `<div class="card-h">${icon("thermometer")}<h3>Room comfort</h3>${i.temperature ? modePill(i.temperature.source_mode) : ""}</div>
    <div class="big">${t.ok ? esc(t.text) : pill(t.tone, "Temperature " + t.text.toLowerCase())}</div>
    <div class="sub">${icon("droplet", "inline")}${h.ok ? `${esc(h.text)} humidity` : `Humidity ${esc(h.text.toLowerCase())}`}</div>
    <div class="status ${c.tone}">${icon(c.tone === "ok" ? "leaf" : c.tone === "warn" ? "triangle-alert" : "circle-help")}${esc(c.text)}</div>`;
}

function weatherCard(d) {
  const f = d.forecast;
  // Local hour at the middle of the forecast period decides day or night wording.
  let hour = NaN;
  if (f && f.start && f.end) {
    try {
      const mid = new Date((Date.parse(f.start) + Date.parse(f.end)) / 2);
      hour = Number(mid.toLocaleTimeString("en-US", { hour: "numeric", hourCycle: "h23", timeZone: zone() }));
    } catch (e) { /* unknown zone: day wording */ }
  }
  const w = L.weatherVisual(f, L.isNightHour(hour));
  const head = `<div class="card-h">${icon("cloud")}<h3>Weather</h3>${f ? modePill(f.source_mode) : ""}</div>`;
  if (!w.known) {
    return head + `<div class="wx unknown">${icon(w.icon, "wx-ic muted")}<div class="state-msg">${esc(w.text)}</div></div>
      <div class="meta">${f && f.error ? esc(L.cap(f.error)) + ". " : ""}Indoor readings never replace the forecast.</div>`;
  }
  const temp = L.fmtTemp(f.temperature_c, units());
  return head + `<div class="wx"><div><div class="big">${temp ? esc(temp) : "—"}</div>
      <div class="sub">${esc(w.text)}${f.precipitation_probability != null ? ` · ${f.precipitation_probability}% chance of rain` : ""}</div></div>
      ${icon(w.icon, "wx-ic")}</div>
    <div class="meta">${icon("map-pin", "inline")}${esc(f.location || "Location unknown")}<span class="dot">·</span>${esc(clockOf(f.start))}–${esc(clockOf(f.end))}${
      /Open-Meteo/.test(f.label || "") ? '<span class="dot">·</span>Data: Open-Meteo.com' : ""}</div>`;
}

function sensorsCard(d) {
  const i = d.indoor;
  const tile = (metric, ic, label) => {
    const sample = i[metric];
    const v = L.sampleView(metric, sample, units());
    return `<div class="tile">
      <div class="t-h">${icon(ic)}${esc(label)}</div>
      <div class="t-v">${v.ok ? esc(v.text) : pill(v.tone, v.text)}</div>
      <div class="t-m">${sample ? modePill(sample.source_mode) : ""}${!v.ok && v.reason ? `<span class="why-na" title="${esc(v.reason)}">${esc(L.humanize(v.reason))}</span>` : ""}</div>
    </div>`;
  };
  const disp = d.display;
  const pct = Math.round(disp.screen_level * 100);
  const how = d.settings.brightness_override != null ? "Manual brightness"
    : disp.source === "ambient_light" ? `Follows ambient light${disp.calibrated ? "" : " (uncalibrated)"}` : "Default level (no light reading)";
  const p = L.proximityView(i.proximity);
  const c = L.comfortView(i, d.settings);
  const hw = d.gateway_mode !== "simulated";
  return `<div class="card-h">${icon("activity")}<h3>Live sensor activity</h3></div>
    <div class="sub"><span>${hw ? "Readings from the UNO Q Modulinos" : "Readings from the simulated gateway"}</span><span class="status ${c.tone}">${icon("leaf")}${esc(c.text)}</span></div>
    <div class="tiles">
      ${tile("temperature", "thermometer", "Temperature")}${tile("humidity", "droplet", "Humidity")}
      ${tile("ambient_light", "sun-dim", "Ambient light")}${tile("distance", "scan", "Distance")}
    </div>
    <div class="bright">
      <div class="bh"><span>${icon("gauge")}Screen brightness</span><strong>${pct}%</strong></div>
      <div class="bar" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="Screen brightness"><span style="width:${pct}%"></span></div>
      <div class="meta">${esc(how)} · on-screen dimming${disp.awake ? "" : " · screen asleep"}</div>
    </div>
    <div class="strip">${icon("radar")}<span>Proximity</span><span class="grow"></span>${pill(p.tone, p.text, p.icon)}</div>`;
}

/* ---------------------------------------------------------- settings */
function seg(name, value, options) {
  return `<div class="seg" role="group">${options.map(([v, label]) => `<button class="${String(v) === String(value) ? "on" : ""}" aria-pressed="${String(v) === String(value)}" data-set="${name}" data-value='${esc(JSON.stringify(v))}'>${esc(label)}</button>`).join("")}</div>`;
}
function toggle(name, value, label) {
  return `<button class="toggle ${value ? "on" : ""}" role="switch" aria-checked="${!!value}" ${label ? `aria-label="${esc(label)}"` : ""} data-set="${name}" data-value='${JSON.stringify(!value)}'><span class="knob"></span><span>${value ? "On" : "Off"}</span></button>`;
}
function actionToggle(action, value, onText = "On", offText = "Off", label = "") {
  return `<button class="toggle ${value ? "on" : ""}" role="switch" aria-checked="${!!value}" ${label ? `aria-label="${esc(label)}"` : ""} data-action="${action}"><span class="knob"></span><span>${value ? onText : offText}</span></button>`;
}
function stepper(name, value, step, fmt = (v) => v) {
  return `<span class="stepper"><button data-step="${name}" data-delta="${-step}" aria-label="Decrease">−</button><span class="val">${esc(fmt(value))}</span><button data-step="${name}" data-delta="${step}" aria-label="Increase">+</button></span>`;
}
function timeStepper(name, value) {
  return `<span class="stepper"><button data-tstep="${name}" data-delta="-30" aria-label="30 minutes earlier">−</button><span class="val">${esc(L.fmt12(value))}</span><button data-tstep="${name}" data-delta="30" aria-label="30 minutes later">+</button></span>`;
}
const field = (name, sub, control) => `<div class="field"><div class="name">${name}${sub ? `<small>${sub}</small>` : ""}</div><div class="ctl">${control}</div></div>`;
const group = (title, body, ic = "") => `<section class="group"><h3>${ic ? icon(ic) : ""}${title}</h3>${body}</section>`;
const yesNo = (v, yes = "Yes", no = "No") => v ? `<span class="yn ok">${icon("circle-check")}${yes}</span>` : `<span class="yn bad">${icon("circle-alert")}${no}</span>`;

function renderSettings(d) {
  put($("#settings-nav"), `<h2>Settings</h2>` + SECTIONS.map(([id, label, ic]) =>
    `<button data-section="${id}" class="${state.section === id ? "on" : ""}" aria-current="${state.section === id ? "page" : "false"}">${icon(ic)}<span>${esc(label)}</span>${id === "status" && d.health.some(L.isProblem) ? `<span class="badge">${d.health.filter(L.isProblem).length}</span>` : ""}</button>`).join(""));
  const fn = { general: secGeneral, sources: secSources, sound: secSound, focus: secFocus, status: secStatus, developer: secDeveloper }[state.section];
  const title = SECTIONS.find((s) => s[0] === state.section);
  put($("#settings-pane"), `<div class="pane-h">${icon(title[2])}<h2>${esc(title[1])}</h2></div>` + fn(d));
  // The query lives in view state; keep the box in step without stealing focus.
  const box = $("#loc-q");
  if (box && document.activeElement !== box && box.value !== state.locQuery) box.value = state.locQuery;
}

function secGeneral(d) {
  const s = d.settings;
  return group("Display", `
    ${field("Temperature units", "", seg("units", s.units, [["metric", "°C"], ["imperial", "°F"]]))}
    ${field("Time zone", "", seg("time_zone", s.time_zone, [["America/Chicago", "Central"], ["America/New_York", "Eastern"], ["America/Los_Angeles", "Pacific"], ["UTC", "UTC"]]))}
    ${field("Brightness", s.brightness_override == null ? "Automatic from the Light sensor (uncalibrated). Dims the picture on screen; the panel backlight is not controlled." : "Manual level, dimmed on screen", seg("brightness_override", s.brightness_override ?? "auto", [["auto", "Auto"], [0.4, "40%"], [0.7, "70%"], [1, "100%"]]))}`, "monitor")
    + group("Stored data", field("Notices, evidence and logs", "Clearing keeps your settings", `<button class="btn danger" data-action="clear-data">${icon("trash-2")}Clear</button>`), "database");
}

function secSources(d) {
  const s = d.settings, sc = d.schedule, f = d.forecast;
  const sourceState = (x) => !x ? pill("muted", "Not configured") : x.quality === "valid" ? pill("ok", "Working", "circle-check")
    : pill(x.quality === "stale" ? "warn" : "bad", x.quality === "stale" ? "Out of date" : "Unavailable");
  const loc = s.location;
  const live = s.weather_source === "open_meteo";
  const link = d.calendar_link || {};
  const liveCal = s.calendar_source !== "fixture";
  return group("Calendar", `
    ${field("Calendar source", !liveCal ? "Fictional demo calendar"
      : link.connected ? `Your calendar, via its subscription link (${esc(link.host || "")}), refreshed every 10 minutes`
      : "Live calendar: connect a subscription link below",
      link.connected ? seg("calendar_source", liveCal ? "live" : "fixture", [["fixture", "Demo"], ["live", "Live"]])
        : seg("calendar_source", s.calendar_source === "fixture" ? "fixture" : "live", [["fixture", "Demo"], ["live", "Live"]]))}
    ${field("Status", sc ? esc(sc.label) + (sc.error ? " · " + esc(L.healthHint({ reason: sc.error })) : "") : "", sourceState(sc))}
    ${calendarConnect(link)}`, "calendar")
    + group("Weather", `
    ${field("Weather source", !live ? "Fictional demo forecast" : loc ? "Live forecast from Open-Meteo.com, refreshed every 30 minutes"
      : "Live forecast from Open-Meteo.com: choose a location below", seg("weather_source", s.weather_source, [["fixture", "Demo"], ["open_meteo", "Live (Open-Meteo)"]]))}
    ${field("Status", f ? esc(f.label) + (f.error ? " · " + esc(L.healthHint({ reason: f.error })) : "") : "", sourceState(f))}`, "cloud")
    + group("Forecast location", `
    ${field("Location", loc ? `Chosen by you · time zone ${esc(loc.time_zone)}` : "Not set. ContextCue never guesses your location.",
      `<strong>${esc(loc ? [loc.name, loc.region, loc.country].filter(Boolean).join(", ") : "None")}</strong>`)}
    ${locationSearch()}`, "map-pin");
}

function providerFor(host) {
  host = (host || "").toLowerCase();
  if (host.endsWith("calendar.google.com")) return "google";
  if (host.startsWith("outlook.") || host.endsWith("office365.com") || host.endsWith("outlook.com")) return "microsoft365";
  return "ics";
}

function calendarConnect(link) {
  if (link.connected) {
    return field("Subscription link", `Connected to ${esc(link.host || "a calendar")}. The link is stored on the board and never shown.`,
      `<button class="btn" data-action="cal-disconnect">${icon("x")}Disconnect</button>`);
  }
  return `<div class="field col"><div class="name">Connect your calendar<small>
      Google Calendar (web): Settings → your calendar → <em>Secret address in iCal format</em>.
      Outlook: Settings → Calendar → Shared calendars → <em>Publish a calendar</em> → ICS link.
      Paste it here from a laptop (SSH tunnel), or on the board run <code>scripts/contextcue-app.sh calendar-link</code>.
      The link gives read access to your calendar: keep it private.</small></div>
    <div class="loc-search"><input id="cal-link" type="password" autocomplete="off" spellcheck="false" placeholder="https://… or webcal://…" aria-label="Calendar subscription link">
      <button class="btn primary" data-action="cal-connect" ${state.calBusy ? "disabled" : ""}>${icon("calendar")}${state.calBusy ? "Checking…" : "Connect"}</button></div></div>`;
}

const KEY_ROWS = ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM-'.,"];
function locationSearch() {
  const results = state.locResults;
  const list = state.locBusy ? '<p class="note">Searching…</p>'
    : state.locError ? `<p class="note bad-text">${esc(state.locError)}</p>`
    : results == null ? ""
    : results.length ? `<div class="loc-results">${results.map((r, i) => `<button class="btn loc-result" data-loc-index="${i}">${icon("map-pin")}<span>${esc([r.name, r.region, r.country].filter(Boolean).join(", "))}</span><small>${esc(r.time_zone)}</small></button>`).join("")}</div>`
    : '<p class="note">No places found. Check the spelling.</p>';
  const keyboard = state.locKeyboard ? `<div class="osk">
      ${KEY_ROWS.map((row) => `<div class="osk-row">${[...row].map((k) => `<button class="osk-key" data-key="${k}">${k}</button>`).join("")}</div>`).join("")}
      <div class="osk-row"><button class="osk-key wide" data-key="space">Space</button><button class="osk-key" data-key="back" aria-label="Delete">⌫</button><button class="osk-key" data-key="clear">Clear</button><button class="osk-key go" data-action="loc-search">Search</button></div>
    </div>` : "";
  return `<div class="field col"><div class="name">Find a city<small>Type a city name, search, then tap the right place.</small></div>
    <div class="loc-search"><input id="loc-q" type="text" autocomplete="off" spellcheck="false" placeholder="Tap to type a city" aria-label="City name" value="${esc(state.locQuery)}">
      <button class="btn" data-action="loc-kb-toggle" aria-pressed="${state.locKeyboard}">${state.locKeyboard ? "Hide keys" : "Keyboard"}</button>
      <button class="btn primary" data-action="loc-search">${icon("map-pin")}Search</button></div>
    ${keyboard}${list}</div>`;
}

async function searchLocation() {
  const query = state.locQuery.trim();
  if (query.length < 2) { toast("Type at least 2 letters", true); return; }
  state.locBusy = true; state.locError = ""; state.locResults = null; render();
  const res = await act("location_search", { query }, true);
  state.locBusy = false;
  if (res) state.locResults = res.results;
  else state.locError = "Search failed. Check the board's network connection.";
  render();
}

function secSound(d) {
  const a = d.audio, s = d.settings;
  return group("Speaker and speech", `
    ${field("Speech engine", "", `<span>${esc(a.engine || "None installed")}</span>`)}
    ${field("Speaker", "Plug in a USB speaker or pair a Bluetooth one on the board; the board's chosen sound output is used", `<span>${esc(a.endpoint || "None selected")}</span>`)}
    ${field("Ready to play", a.reason ? esc(L.healthHint({ reason: a.reason })) : "", yesNo(a.ready))}
    <p class="note">Notices are spoken only when Audio below allows it. Finished playback does not prove anyone heard it.</p>`, "volume-2")
    + group("Sound", `
    ${field("Mute", "Silences speech and the Buzzer; notices stay on screen", actionToggle("toggle-mute", d.status.muted, "Muted", "Off", "Mute"))}
    ${field("Speech volume", `<span id="vol-val">${a.volume}%</span>`, `<input id="volume" type="range" min="0" max="100" step="5" value="${a.volume}" aria-label="Speech volume">`)}
    ${field("Audio", "When notices may be spoken", seg("audio_policy", a.policy, [["visual_only", "Visual only"], ["on_request", "On request"], ["automatic", "Automatic"]]))}
    ${field("Buzzer chime", "Separate opt-in; one short tone only", toggle("buzzer_enabled", a.buzzer_enabled, "Buzzer chime"))}
    ${field("Away announcements", "Once, and only to a connected speaker", toggle("away_audio_enabled", s.away_audio_enabled, "Away announcements"))}
    ${field("Stop audio", d.status.playing ? "Playing now" : "Nothing playing", `<button class="btn" data-action="stop-audio">${icon("square")}Stop</button>`)}`, "volume-2")
    + group("Quiet hours", field("From – to", "No automatic speech or Buzzer", `${timeStepper("quiet_start", s.quiet_start)}<span class="to">to</span>${timeStepper("quiet_end", s.quiet_end)}`), "moon-star");
}

function secFocus(d) {
  const s = d.settings;
  return group("Focus", `
    ${field("Focus", "Holds unsolicited briefings and automatic speech until you turn it off", actionToggle("toggle-focus", d.status.focus, "On", "Off", "Focus"))}
    ${field("Speech during Focus", "Urgent preparation reminders only", toggle("focus_p2_speech", s.focus_p2_speech, "Speech during Focus"))}`, "moon")
    + group("Morning briefing", field("Window", "Briefing when you approach the desk, once per day", `${timeStepper("morning_start", s.morning_start)}<span class="to">to</span>${timeStepper("morning_end", s.morning_end)}`), "sunrise")
    + group("Comfort range", `
    ${field("Lowest temperature", "", stepper("comfort_temp_min_c", s.comfort_temp_min_c, 0.5, (v) => L.fmtTemp(v, s.units)))}
    ${field("Highest temperature", "", stepper("comfort_temp_max_c", s.comfort_temp_max_c, 0.5, (v) => L.fmtTemp(v, s.units)))}
    ${field("Lowest humidity", "", stepper("comfort_rh_min", s.comfort_rh_min, 5, (v) => v.toFixed(0) + " %"))}
    ${field("Highest humidity", "", stepper("comfort_rh_max", s.comfort_rh_max, 5, (v) => v.toFixed(0) + " %"))}`, "thermometer");
}

function secStatus(d) {
  const rows = [...d.health].sort((a, b) => L.isProblem(b) - L.isProblem(a)).map((h) => {
    const v = L.healthVisual(h.state);
    const hint = L.healthHint(h);
    return `<div class="hrow ${v.tone}">
      ${icon(v.icon, "h-ic")}
      <div class="h-main"><div class="h-name">${esc(L.componentName(h.component_id))}</div>
        ${hint ? `<div class="h-hint">${esc(hint)}</div>` : ""}
        ${state.techOpen ? `<div class="h-tech">${esc(h.component_id)} · ${esc(h.state)}${h.reason ? " · " + esc(h.reason) : ""}${h.execution_location ? " · " + esc(L.humanize(h.execution_location)) : ""}${h.last_success_at ? " · last OK " + esc(clockOf(h.last_success_at, true)) : ""}</div>` : ""}</div>
      ${pill(v.tone, v.text)}</div>`;
  }).join("");
  const problems = d.health.filter(L.isProblem).length;
  return `<div class="status-sum ${problems ? "warn" : "ok"}">${icon(problems ? "triangle-alert" : "circle-check")}<span>${problems ? `${problems} component${problems > 1 ? "s need" : " needs"} attention. Everything else keeps working.` : "Everything is working."}</span></div>`
    + group("Components", rows + `<button class="link" data-action="tech-toggle" aria-expanded="${state.techOpen}">${state.techOpen ? "Hide" : "Show"} technical details</button>`, "activity")
    + (d.startup_notes.length ? group("Startup notes", `<ul class="plain">${d.startup_notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`, "info") : "");
}

function secDeveloper(d) {
  const sim = d.simulation;
  const simSensors = d.gateway_mode === "simulated";
  const fictional = d.settings.calendar_source === "fixture" || d.settings.weather_source === "fixture";
  const combo = `${simSensors ? "Simulated sensors" : "Real sensors"} + ${fictional ? "fictional calendar/weather" : "live calendar/weather"}`;
  let html = group("Demo Mode", field("Demo Mode", "Adds the live sensor panel to Glance. Display only: it changes no data. Remembered on this screen only.", actionToggle("demo-toggle", state.demo, "On", "Off", "Demo Mode")), "monitor");

  const scen = sim.scenarios.map((x) => `<button class="scen ${x.scenario_id === sim.scenario_id ? "on" : ""}" aria-pressed="${x.scenario_id === sim.scenario_id}" data-set="scenario" data-value='${esc(JSON.stringify(x.scenario_id))}'><strong>${esc(x.title)}</strong></button>`).join("");
  html += group("Scenario testing", `
    ${field("Sensor source", "Chosen when the app starts (<code>--simulated</code>). Restart the app to change it.", simSensors ? pill("sim", "Simulated gateway", "flask-conical") : pill("real", "Real UNO Q sensors", "cpu"))}
    <div class="field col"><div class="name">Calendar and weather scenario<small>${sim.time_controls ? esc(sim.scenario_description)
      : "On the device clock the chosen scenario's fictional calendar and forecast replay from now, so its events are always ahead. Tap a scenario again to restart it."}</small></div><div class="scens">${scen}</div></div>
    ${sim.time_controls ? field("Scenario clock", `Now ${esc(L.fmt12(d.clock.local_time))} (simulated)`, `<span class="btns"><button class="btn" data-advance="60">+1 min</button><button class="btn" data-advance="600">+10 min</button><button class="btn" data-advance="3600">+1 hour</button></span>`) : ""}
    <div class="combo ${simSensors ? "sim" : "real"}">${icon("circle-check")}<span>${esc(combo)}</span></div>`, "flask-conical");

  if (sim.sensor_controls) {
    const v = sim.values, f = sim.faults;
    const slider = (metric, label, min, max, step, unit) => field(label, `<span id="sv-${metric}">${v[metric]} ${unit}</span>`,
      `<input type="range" data-sensor="${metric}" min="${min}" max="${max}" step="${step}" value="${v[metric]}" aria-label="${esc(label)}">`);
    const fault = (target, label) => field(label, f[target] ? esc(L.cap(f[target])) : "Working",
      `<button class="toggle ${f[target] ? "on bad" : ""}" role="switch" aria-checked="${!!f[target]}" data-fault="${target}" data-active="${!f[target]}"><span class="knob"></span><span>${f[target] ? "Failed" : "OK"}</span></button>`);
    html += group("Simulated readings", `
      ${slider("temperature", "Temperature", 10, 35, 0.5, "°C")}${slider("humidity", "Humidity", 10, 90, 1, "%RH")}
      ${slider("ambient_light", "Light", 0, 1000, 10, "lux")}${slider("distance", "Distance", 100, 2000, 50, "mm")}
      ${field("Distance shortcuts", "", `<span class="btns"><button class="btn" data-sensor-set="distance" data-value="350">Approach</button><button class="btn" data-sensor-set="distance" data-value="1200">Leave</button></span>`)}`, "sliders-horizontal")
      + group("Fault injection", `
      ${fault("thermo", "Thermo")}${fault("light", "Light")}${fault("distance", "Distance")}
      ${fault("bridge", "Bridge")}${fault("calendar", "Calendar source")}${fault("weather", "Weather source")}
      ${field("MCU reboot", "Simulates a new boot identity", `<button class="btn" data-fault="reboot" data-active="true">${icon("refresh-cw")}Reboot</button>`)}`, "triangle-alert");
  } else {
    html += group("Real hardware", `<p class="note">Sensor values come from the real gateway. Only the scenario clock can be simulated; sensors are never faked on the hardware path.</p>`, "cpu");
  }

  html += group("Diagnostics", `<button class="link" data-action="diag-toggle" aria-expanded="${state.diagOpen}">${state.diagOpen ? "Hide" : "Show"} recent activity log</button>
    ${state.diagOpen ? `<div class="diag">${d.diagnostics.map((x) => { const { at, kind, ...rest } = x; return `<div><span class="k">${esc(clockOf(at, true))} ${esc(kind)}</span> ${esc(JSON.stringify(rest))}</div>`; }).join("") || "Nothing logged yet."}</div>` : ""}`, "code-xml");
  return html;
}

/* ------------------------------------------------------ sheets/dialog */
function renderSheet(d) {
  const el = $("#sheet");
  el.hidden = !state.sheet;
  if (!state.sheet) return;
  const close = `<button class="icon-btn" data-close="sheet" aria-label="Close">${icon("x")}</button>`;
  let html;
  if (state.sheet === "ask") {
    html = `<div class="sheet-h"><h2 id="sheet-title">${icon("sparkles")}Ask ContextCue</h2>${close}</div>
      <p class="note">Answers use only current calendar, weather and sensor data. Other questions are not answered.</p>
      <button class="btn primary wide" data-action="briefing">${icon("sparkles")}Brief me now</button>
      <div class="qgrid">${Object.entries(d.questions).map(([id, text]) => `<button class="btn q" data-action="ask" data-id="${esc(id)}">${icon("circle-help")}${esc(text)}</button>`).join("")}</div>`;
  } else {
    const list = L.otherNotices(d.notices, d.notice && d.notice.notice_id);
    html = `<div class="sheet-h"><h2 id="sheet-title">${icon("bell")}Other notices</h2>${close}</div>
      <div class="nlist">${list.map((n) => `<div class="nrow">
        <div class="n-h"><strong>${esc(L.CATEGORY[n.category] || L.cap(n.category))}</strong>${pill("muted", L.cap(n.lifecycle))}${modePill(n.source_mode)}</div>
        <div>${esc(n.text || (n.lifecycle === "deferred" ? "Waiting until Focus is off" : "Preparing…"))}</div>
        ${n.lifecycle === "active" ? `<button class="btn" data-action="ack" data-id="${esc(n.notice_id)}">${icon("check")}Done</button>` : ""}
      </div>`).join("") || '<p class="note">No other notices.</p>'}</div>`;
  }
  put(el.firstElementChild, html);
}

function renderDialog() {
  const el = $("#dialog");
  el.hidden = !state.dialog;
  if (!state.dialog) return;
  const x = state.dialog;
  put(el.firstElementChild, `<h2 id="dialog-title">${esc(x.title)}</h2><p>${esc(x.text)}</p>
    <div class="actions"><button class="btn" data-close="dialog">Cancel</button><button class="btn ${x.danger ? "danger-fill" : "primary"}" data-action="dialog-ok">${esc(x.ok)}</button></div>`);
}

function confirmDialog(title, text, ok, run, danger = false) {
  state.dialog = { title, text, ok, run, danger };
  renderDialog();
  $("#dialog .actions .btn:last-child").focus();
}

/* ------------------------------------------------------------ events */
function showTab(tab) {
  state.tab = tab;
  $("#frame").dataset.tab = tab;
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.id === `tab-${tab}`));
  document.querySelectorAll("#tabbar button").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
    el.setAttribute("aria-current", el.dataset.tab === tab ? "page" : "false");
  });
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

async function play(id) {
  const d = state.data;
  const n = d.notices.find((x) => x.notice_id === id);
  if (n && n.play_blocked_reason === "audio_policy_visual_only") {
    confirmDialog("Audio is set to visual only", "Switch to “audio on request” and play this notice?", "Switch and play", async () => {
      if (await act("settings", { changes: { audio_policy: "on_request" } }, true)) await act("play", { notice_id: id });
    });
    return;
  }
  return act("play", { notice_id: id });
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  const now = Date.now();
  if (now - state.lastTouch > 5000) { state.lastTouch = now; act("touch", {}, true); }
  // Tapping the dimmed backdrop closes a sheet.
  if (!t && ev.target.id === "sheet") { state.sheet = null; return render(); }
  if (!t || !state.data || t.disabled) return;
  const d = state.data;
  if (t.dataset.tab) return showTab(t.dataset.tab);
  if (t.dataset.goto) { state.section = t.dataset.goto; return showTab("settings"); }
  if (t.dataset.section) { state.section = t.dataset.section; $("#settings-pane").scrollTop = 0; return render(); }
  if (t.dataset.sheet) { state.sheet = t.dataset.sheet; return render(); }
  if (t.dataset.close) { state[t.dataset.close] = null; return render(); }
  if (t.dataset.set) {
    let value = JSON.parse(t.dataset.value);
    if (t.dataset.set === "scenario") return act("sim_scenario", { scenario_id: value });
    if (t.dataset.set === "brightness_override" && value === "auto") value = null;
    if (t.dataset.set === "calendar_source" && value === "live") {
      if (!(d.calendar_link || {}).connected) { toast("Connect a calendar link first", true); return; }
      value = providerFor(d.calendar_link.host);
    }
    return act("settings", { changes: { [t.dataset.set]: value } });
  }
  if (t.dataset.step) return setStep(t.dataset.step, Number(t.dataset.delta));
  if (t.dataset.tstep) return setTimeStep(t.dataset.tstep, Number(t.dataset.delta));
  if (t.dataset.advance) return act("sim_advance", { seconds: Number(t.dataset.advance) });
  if (t.dataset.fault) return act("sim_fault", { target: t.dataset.fault, active: t.dataset.active === "true" });
  if (t.dataset.sensorSet) return act("sim_sensor", { metric: t.dataset.sensorSet, value: Number(t.dataset.value) });
  if (t.dataset.key) {
    const k = t.dataset.key;
    state.locQuery = k === "back" ? state.locQuery.slice(0, -1) : k === "clear" ? ""
      : k === "space" ? state.locQuery + " "
      : state.locQuery + (/(^|[\s-])$/.test(state.locQuery) ? k : k.toLowerCase());  // "Winston-Salem"
    if (state.locQuery.length > 60) state.locQuery = state.locQuery.slice(0, 60);
    const box = $("#loc-q");
    if (box) box.value = state.locQuery;
    if (state.locQuery.length >= 3) scheduleLocSearch();  // results appear as you type
    return;
  }
  if (t.dataset.locIndex) {
    const place = (state.locResults || [])[Number(t.dataset.locIndex)];
    if (!place) return;
    const res = await act("set_location", { location: place }, true);
    if (res) {
      toast(`Forecast location: ${place.name}`);
      state.locResults = null; state.locQuery = ""; state.locKeyboard = false;
    }
    return render();
  }
  switch (t.dataset.action) {
    case "toggle-focus": return act("focus", { enabled: !d.status.focus });
    case "toggle-mute": return act("mute", { muted: !d.status.muted });
    case "ack": {
      const res = await act("acknowledge", { notice_id: t.dataset.id });
      if (res && state.whyOpen === t.dataset.id) state.whyOpen = null;
      return render();
    }
    case "play": return play(t.dataset.id);
    case "why": state.whyOpen = state.whyOpen === t.dataset.id ? null : t.dataset.id; return render();
    case "why-details": state.whyDetails = !state.whyDetails; return render();
    case "briefing": state.sheet = null; render(); return act("briefing");
    case "ask": state.sheet = null; render(); return act("ask", { question: t.dataset.id });
    case "stop-audio": return act("stop_audio");
    case "clear-data":
      return confirmDialog("Clear stored data?", "Notices, evidence and logs will be deleted. Your settings are kept.", "Clear data", () => act("clear_data"), true);
    case "dialog-ok": { const run = state.dialog && state.dialog.run; state.dialog = null; render(); return run && run(); }
    case "demo-toggle": state.demo = !state.demo; saveDemo(state.demo); toast(state.demo ? "Demo Mode on: sensor panel shown on Glance" : "Demo Mode off"); return render();
    case "diag-toggle": state.diagOpen = !state.diagOpen; return render();
    case "tech-toggle": state.techOpen = !state.techOpen; return render();
    case "loc-kb-toggle": state.locKeyboard = !state.locKeyboard; return render();
    case "loc-search": return searchLocation();
    case "cal-connect": {
      const box = $("#cal-link");
      const value = ((box && box.value) || state.calLink).trim();
      if (!value) { toast("Paste the calendar's subscription link first", true); return; }
      state.calBusy = true; render();
      const res = await act("calendar_connect", { link: value }, true);
      state.calBusy = false;
      if (res) { state.calLink = ""; toast(`Calendar connected: ${res.events} events today and tomorrow`); }
      return render();
    }
    case "cal-disconnect":
      return confirmDialog("Disconnect calendar?", "The stored link is deleted and the demo calendar is used.", "Disconnect", () => act("calendar_disconnect"), true);
  }
});

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (state.dialog) state.dialog = null; else if (state.sheet) state.sheet = null; else return;
  render();
});

// Optional starting view from the URL (screenshots, kiosk launch):
// ?tab=settings&section=developer&demo=1&why=1&sheet=ask. Display state only;
// a demo= value here is not saved.
function readUrlState() {
  const q = new URLSearchParams(location.search);
  if (q.get("tab") === "settings") { state.tab = "settings"; }
  if (SECTIONS.some((s) => s[0] === q.get("section"))) state.section = q.get("section");
  if (q.has("demo")) state.demo = q.get("demo") === "1";
  if (q.get("why") === "1") state.whyOpen = "__selected__";
  if (q.get("details") === "1") state.whyDetails = true;
  if (["ask", "notices"].includes(q.get("sheet"))) state.sheet = q.get("sheet");
  showTab(state.tab);
}

document.addEventListener("pointerdown", (ev) => { if (ev.target.type === "range") state.dragging = true; });
document.addEventListener("pointerup", () => { state.dragging = false; });
document.addEventListener("keydown", (ev) => {
  if (ev.target.id === "loc-q" && ev.key === "Enter") searchLocation();
});
// Tapping the city field opens the on-screen keyboard: the kiosk has no
// physical keyboard and Chromium shows no system one.
document.addEventListener("focusin", (ev) => {
  if (ev.target.id === "loc-q" && !state.locKeyboard) { state.locKeyboard = true; render(); }
});
let locTimer = null;
function scheduleLocSearch() {
  clearTimeout(locTimer);
  locTimer = setTimeout(() => { if (state.locQuery.trim().length >= 3) searchLocation(); }, 700);
}
document.addEventListener("input", (ev) => {
  if (ev.target.id === "loc-q") state.locQuery = ev.target.value.slice(0, 60);
  if (ev.target.id === "cal-link") state.calLink = ev.target.value.slice(0, 2048);
  if (ev.target.id === "volume") $("#vol-val").textContent = `${ev.target.value}%`;
  if (ev.target.dataset.sensor) {
    const u = { temperature: "°C", humidity: "%RH", ambient_light: "lux", distance: "mm" };
    $(`#sv-${ev.target.dataset.sensor}`).textContent = `${ev.target.value} ${u[ev.target.dataset.sensor]}`;
  }
});
document.addEventListener("change", (ev) => {
  state.dragging = false;
  if (ev.target.id === "volume") act("volume", { volume: Number(ev.target.value) });
  if (ev.target.dataset.sensor) act("sim_sensor", { metric: ev.target.dataset.sensor, value: Number(ev.target.value) }, true);
});

// Inline the local icon sprite once; <use href="#name"> picks it up when it lands.
async function loadIcons() {
  try {
    const res = await fetch("icons/sprite.svg");
    if (!res.ok) return;
    const holder = document.createElement("div");
    holder.setAttribute("aria-hidden", "true");
    holder.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
    holder.innerHTML = await res.text();
    document.body.prepend(holder);
  } catch (e) { /* icons stay blank; every state is also written as text */ }
}

loadIcons();
readUrlState();
poll();
