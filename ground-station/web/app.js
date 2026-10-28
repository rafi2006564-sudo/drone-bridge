"use strict";

// Timing. The backend treats the UI as gone after 0.5 s without a heartbeat.
const HEARTBEAT_MS = 100;
const STICK_MS = 50;
const GIMBAL_MS = 200;
const GIMBAL_RATE = 20; // deg/s while R/F is held
const RAMP_UP = 4; // stick units per second when a key is pressed
const RAMP_DOWN = 8; // ... and when released
const TRACK_POINTS = 2000;

// Mode 2: left stick = yaw (x) / throttle (y), right stick = roll (x) / pitch (y).
const KEYMAP = {
  KeyW: ["ly", 1], KeyS: ["ly", -1], KeyA: ["lx", -1], KeyD: ["lx", 1],
  ArrowUp: ["ry", 1], ArrowDown: ["ry", -1], ArrowLeft: ["rx", -1], ArrowRight: ["rx", 1],
};
const GIMBAL_KEYS = { KeyR: 1, KeyF: -1 };
const AXES = ["lx", "ly", "rx", "ry"];

const CONFIRM_TEXT = {
  takeoff: "Take off? The aircraft will start its motors, climb a short height and hover.",
  enable_virtual_stick:
    "Enable virtual stick? The PC will control the aircraft. The pilot can take over with the RC at any time.",
  mission_start: "Start the uploaded mission? The aircraft flies it by itself; the pilot can pause or take over with the RC.",
};
const QUIET = new Set(["gimbal_rotate"]); // not echoed in the event log

const state = {
  ws: null,
  rejected: false,
  config: null,
  status: null,
  telemetry: null,
  hbSeq: 0,
  cmdSeq: 0,
  pending: new Map(), // id -> name
  held: new Set(),
  sticks: { lx: 0, ly: 0, rx: 0, ry: 0 },
  gimbalDir: 0,
  dialogOpen: false,
  missions: [],
  schedules: [],
  serverOffset: 0, // server Unix time minus browser Unix time, seconds
  pendingOps: new Map(), // id -> resolve
  selectedMission: "",
  editing: null, // {id, name, speed, finish_action, waypoints: [{lat, lon, alt, speed, photo, gimbal}]}
};

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ socket

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => setPill("pill-backend", "ok", "backend");
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    state.ws = null;
    state.telemetry = null;
    state.status = null;
    setPill("pill-backend", "bad", "backend: disconnected");
    renderStatus();
    if (!state.rejected) setTimeout(connect, 1000);
  };
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
}

function handle(msg) {
  switch (msg.type) {
    case "hello":
      applyConfig(msg.config);
      break;
    case "status":
      state.status = msg;
      renderStatus();
      break;
    case "telemetry":
      state.telemetry = msg;
      renderTelemetry(msg);
      break;
    case "event":
      logEvent(msg.level, `${msg.code}: ${msg.message}`);
      break;
    case "missions":
      state.missions = msg.missions;
      renderMissions();
      break;
    case "schedules":
      state.schedules = msg.schedules;
      state.serverOffset = msg.server_time - Date.now() / 1000;
      renderSchedules();
      break;
    case "ack": {
      const resolveOp = state.pendingOps.get(msg.id);
      if (resolveOp) {
        state.pendingOps.delete(msg.id);
        if (!msg.ok) logEvent("fail", `${msg.name} failed: ${msg.error}`);
        resolveOp(msg);
        break;
      }
      const name = state.pending.get(msg.id) || msg.name;
      state.pending.delete(msg.id);
      if (!msg.ok) logEvent("fail", `${name} failed: ${msg.error}`);
      else if (!QUIET.has(name)) logEvent("info", `${name}: ok`);
      break;
    }
    case "rejected":
      state.rejected = true;
      showBanner(`Not in control: ${msg.reason}. Close the other tab, then reload this one.`);
      break;
  }
}

// ------------------------------------------------------------------ heartbeat & sticks

setInterval(() => send({ type: "ui_heartbeat", seq: state.hbSeq++ }), HEARTBEAT_MS);

let lastStickTick = performance.now();
setInterval(() => {
  const now = performance.now();
  const dt = Math.min(0.2, (now - lastStickTick) / 1000);
  lastStickTick = now;
  updateSticks(dt);
  const db = state.config ? state.config.deadband : 0.05;
  const out = {};
  for (const a of AXES) out[a] = deadband(state.sticks[a], db);
  send({ type: "stick", ...out });
  drawSticks(out);
}, STICK_MS);

setInterval(() => {
  if (state.gimbalDir !== 0) sendGimbalRate(state.gimbalDir * GIMBAL_RATE);
}, GIMBAL_MS);

function virtualStickActive() {
  return !!(state.telemetry && state.telemetry.virtual_stick_enabled);
}

function updateSticks(dt) {
  const target = { lx: 0, ly: 0, rx: 0, ry: 0 };
  if (virtualStickActive() && !state.dialogOpen) {
    for (const code of state.held) {
      const m = KEYMAP[code];
      if (m) target[m[0]] = Math.max(-1, Math.min(1, target[m[0]] + m[1]));
    }
  }
  for (const a of AXES) {
    const v = state.sticks[a];
    const t = target[a];
    const growing = Math.abs(t) > Math.abs(v) && Math.sign(t) !== -Math.sign(v);
    const step = (growing ? RAMP_UP : RAMP_DOWN) * dt;
    state.sticks[a] = Math.abs(t - v) <= step ? t : v + Math.sign(t - v) * step;
  }
}

function deadband(v, d) {
  const a = Math.abs(v);
  if (!Number.isFinite(v) || a < d) return 0;
  return Math.sign(v) * Math.min(1, (a - d) / (1 - d));
}

function releaseAll() {
  state.held.clear();
  if (state.gimbalDir !== 0) {
    state.gimbalDir = 0;
    sendGimbalRate(0);
  }
}

function sendGimbalRate(rate) {
  sendCommand("gimbal_rotate", { pitch_rate: rate, yaw_rate: 0 }, false);
}

function isTyping(e) {
  const t = e.target;
  return t && (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || t.isContentEditable);
}

window.addEventListener("keydown", (e) => {
  if (state.dialogOpen || isTyping(e)) return;
  if (e.code === "Space" || e.code === "Escape") {
    releaseAll();
    e.preventDefault();
    return;
  }
  if (KEYMAP[e.code]) {
    state.held.add(e.code);
    e.preventDefault();
  } else if (GIMBAL_KEYS[e.code] && !e.repeat) {
    state.gimbalDir = GIMBAL_KEYS[e.code];
    sendGimbalRate(state.gimbalDir * GIMBAL_RATE);
  }
});

window.addEventListener("keyup", (e) => {
  state.held.delete(e.code);
  if (GIMBAL_KEYS[e.code] && state.gimbalDir === GIMBAL_KEYS[e.code]) {
    state.gimbalDir = 0;
    sendGimbalRate(0);
  }
});

// Key-up events are lost when focus leaves the page; never leave a key stuck.
window.addEventListener("blur", releaseAll);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) releaseAll();
});

// ------------------------------------------------------------------ commands

async function command(name, args = {}) {
  let confirmed = false;
  if (state.config && state.config.confirm_required.includes(name)) {
    confirmed = await confirmDialog(CONFIRM_TEXT[name] || `Send ${name}?`);
    if (!confirmed) return;
  }
  sendCommand(name, args, confirmed);
}

function sendCommand(name, args, confirmed) {
  const id = `u${++state.cmdSeq}`;
  state.pending.set(id, name);
  send({ type: "command", id, name, args, confirmed });
  if (!QUIET.has(name)) logEvent("sent", `→ ${name}`);
}

// A <dialog>, not window.confirm(): confirm() blocks all page scripts, which
// would stop the heartbeat and make the aircraft hover.
function confirmDialog(text) {
  const dlg = $("confirm");
  $("confirm-text").textContent = text;
  releaseAll();
  state.dialogOpen = true;
  dlg.returnValue = "";
  dlg.showModal();
  $("confirm-ok").focus();
  return new Promise((resolve) => {
    dlg.addEventListener(
      "close",
      () => {
        state.dialogOpen = false;
        resolve(dlg.returnValue === "ok");
      },
      { once: true },
    );
  });
}

// Mission and schedule operations; resolves with the backend's ack.
function op(name, args = {}, confirmed = false) {
  const id = `o${++state.cmdSeq}`;
  return new Promise((resolve) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, error: "not connected to the backend" });
      return;
    }
    state.pendingOps.set(id, resolve);
    send({ type: "op", id, op: name, args, confirmed });
  });
}

document.querySelectorAll("button[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const args = btn.dataset.args ? JSON.parse(btn.dataset.args) : {};
    if (btn.dataset.cmd === "simulator_start" && state.config) {
      args.lat = state.config.simulator.lat;
      args.lon = state.config.simulator.lon;
    }
    command(btn.dataset.cmd, args);
  });
});

$("btn-record").addEventListener("click", () => {
  const rec = state.telemetry && state.telemetry.camera_recording;
  command(rec ? "camera_record_stop" : "camera_record_start");
});

$("btn-lens").addEventListener("click", () => {
  const lens = state.telemetry && state.telemetry.camera_lens === "zoom" ? "wide" : "zoom";
  command("camera_lens", { lens });
});

$("btn-zoom").addEventListener("click", () => {
  const ratio = Number($("zoom").value);
  if (Number.isFinite(ratio) && ratio >= 1 && ratio <= 200) command("camera_zoom", { ratio });
});

$("speed").addEventListener("input", (e) => {
  const value = Number(e.target.value);
  $("speed-out").textContent = `${Math.round(value * 100)}%`;
  send({ type: "set_speed_scale", value });
});

// ------------------------------------------------------------------ config & map

let map = null;
let aircraftMarker = null;
let homeMarker = null;
let track = null;

function applyConfig(cfg) {
  state.config = cfg;
  const speed = $("speed");
  speed.max = String(cfg.speed_scale_max);
  if (!state.status) {
    speed.value = String(cfg.speed_scale_default);
    $("speed-out").textContent = `${Math.round(cfg.speed_scale_default * 100)}%`;
  }
  if (cfg.video_player_url && !$("video").querySelector("iframe")) {
    const frame = document.createElement("iframe");
    frame.src = cfg.video_player_url;
    frame.allow = "autoplay";
    $("video").replaceChildren(frame);
  }
  if (!map) initMap(cfg);
}

function initMap(cfg) {
  map = L.map("map").setView([cfg.simulator.lat, cfg.simulator.lon], 17);
  L.tileLayer(cfg.map.tile_url, { maxZoom: 20, maxNativeZoom: 19, attribution: cfg.map.tile_attribution }).addTo(map);
  L.control.scale({ imperial: false }).addTo(map);
  const icon = L.divIcon({
    className: "aircraft-icon",
    iconSize: [28, 28],
    iconAnchor: [14, 14],
    html:
      '<svg viewBox="0 0 28 28"><path d="M14 2 L24 25 L14 19 L4 25 Z" fill="#4d9fff" stroke="#fff" stroke-width="1.5"/></svg>',
  });
  aircraftMarker = L.marker([cfg.simulator.lat, cfg.simulator.lon], { icon, interactive: false });
  homeMarker = L.circleMarker([0, 0], { radius: 7, color: "#3fb950", fillOpacity: 0.6 }).bindTooltip("Home");
  track = L.polyline([], { color: "#4d9fff", weight: 2, opacity: 0.7 }).addTo(map);
  initMissionLayer();
  initSurveyLayer();
  renderMissions();
}

function updateMap(t) {
  if (!map) return;
  if (t.lat !== null && t.lon !== null) {
    const pos = [t.lat, t.lon];
    if (!map.hasLayer(aircraftMarker)) aircraftMarker.addTo(map);
    aircraftMarker.setLatLng(pos);
    const svg = aircraftMarker.getElement() && aircraftMarker.getElement().querySelector("svg");
    if (svg) svg.style.transform = `rotate(${t.heading || 0}deg)`;
    const pts = track.getLatLngs();
    const last = pts[pts.length - 1];
    if (t.is_flying && (!last || map.distance(last, pos) > 0.5)) {
      track.addLatLng(pos);
      if (pts.length > TRACK_POINTS) track.setLatLngs(pts.slice(-TRACK_POINTS));
    }
    if ($("follow").checked) map.panTo(pos, { animate: false });
  }
  if (t.home_set && t.home_lat !== null) {
    homeMarker.setLatLng([t.home_lat, t.home_lon]);
    if (!map.hasLayer(homeMarker)) homeMarker.addTo(map);
  } else if (map.hasLayer(homeMarker)) {
    homeMarker.remove();
  }
}

// ------------------------------------------------------------------ rendering

function setPill(id, cls, text, title = "") {
  const el = $(id);
  el.className = `pill ${cls}`;
  el.textContent = text;
  el.title = title;
}

function renderStatus() {
  const s = state.status;
  const connected = !!(s && s.rc.connected);
  if (!s) {
    setPill("pill-rc", "", "RC");
    setPill("pill-telemetry", "", "telemetry");
    setPill("pill-limits", "", "limits");
    setPill("pill-preflight", "", "pre-flight");
    $("aircraft-info").textContent = "";
  } else {
    setPill("pill-rc", connected ? "ok" : "bad", connected ? "RC connected" : "RC: not connected", s.rc.uri);
    const age = s.telemetry_age;
    if (age === null) setPill("pill-telemetry", "bad", "no telemetry");
    else setPill("pill-telemetry", age < 0.5 ? "ok" : age < 1 ? "warn" : "bad", `telemetry ${age.toFixed(1)} s`);
    setPill("pill-limits", s.limits_applied ? "ok" : "bad", s.limits_applied ? "limits set" : "limits not set");
    const pf = s.preflight;
    setPill(
      "pill-preflight",
      pf.ok ? "ok" : "warn",
      pf.ok ? "takeoff checks pass" : `takeoff blocked (${pf.reasons.length})`,
      pf.reasons.join("\n"),
    );
    const p = s.rc.peer;
    $("aircraft-info").textContent = p
      ? `${p.aircraft_model} · fw ${p.aircraft_firmware} · RC ${p.rc_firmware} · MSDK ${p.msdk_version}`
      : "";
  }
  // "local" buttons (mission editing, scheduling) work without the RC.
  document.querySelectorAll("aside button:not(.local)").forEach((b) => {
    b.disabled = !connected;
  });
}

function fmt(v, digits = 1, unit = "") {
  return v === null || v === undefined ? "–" : `${Number(v).toFixed(digits)}${unit}`;
}

function distanceM(lat1, lon1, lat2, lon2) {
  const r = 6371000;
  const toRad = Math.PI / 180;
  const dn = (lat2 - lat1) * toRad * r;
  const de = (lon2 - lon1) * toRad * r * Math.cos(((lat1 + lat2) / 2) * toRad);
  return Math.hypot(dn, de);
}

function renderTelemetry(t) {
  const hspeed = t.vel_n !== null && t.vel_e !== null ? Math.hypot(t.vel_n, t.vel_e) : null;
  const vspeed = t.vel_d !== null ? -t.vel_d : null;
  const homeDist =
    t.home_set && t.lat !== null && t.home_lat !== null ? distanceM(t.lat, t.lon, t.home_lat, t.home_lon) : null;
  const rows = [
    ["Mode", t.flight_mode],
    ["State", `${t.is_flying ? "flying" : "on ground"}, motors ${t.motors_on ? "on" : "off"}`],
    ["Virtual stick", t.virtual_stick_enabled ? "ENABLED" : "off"],
    ["Altitude", fmt(t.alt_rel, 1, " m")],
    ["Speed", `${fmt(hspeed, 1, " m/s")} horiz, ${fmt(vspeed, 1, " m/s")} vert`],
    ["Heading", fmt(t.heading, 0, "°")],
    ["Battery", fmt(t.battery_pct, 0, "%")],
    ["GPS", `${t.gps_sats} sats, level ${t.gps_level}`],
    ["Home", t.home_set ? `set, ${fmt(homeDist, 0, " m")} away` : "NOT SET"],
    ["Gimbal", `pitch ${fmt(t.gimbal_pitch, 0, "°")}`],
    ["Camera", `${t.camera_lens || "–"} ${fmt(t.zoom_ratio, 1, "x")}${t.camera_recording ? ", REC" : ""}`],
    ["Links", `RC ${fmt(t.rc_link_quality, 0, "%")}, video ${fmt(t.video_link_quality, 0, "%")}`],
    ["Mission", t.mission_state],
    ["Simulator", t.simulator_active ? "active" : "off"],
  ];
  const dl = $("telemetry");
  dl.replaceChildren(
    ...rows.flatMap(([k, v]) => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      return [dt, dd];
    }),
  );
  $("warnings").replaceChildren(
    ...t.active_warnings.map((w) => {
      const li = document.createElement("li");
      li.className = w.level;
      li.textContent = `${w.code}: ${w.message}`;
      return li;
    }),
  );
  $("btn-record").textContent = t.camera_recording ? "Stop recording" : "Record";
  $("btn-record").classList.toggle("active", t.camera_recording);
  $("btn-lens").textContent = `Lens: ${t.camera_lens || "–"}`;
  $("btn-vs-on").classList.toggle("active", t.virtual_stick_enabled);
  $("keys-hint").classList.toggle("muted", !t.virtual_stick_enabled);
  updateMap(t);
}

function drawSticks(s) {
  const place = (el, x, y) => {
    el.style.left = `${50 + x * 45}%`;
    el.style.top = `${50 - y * 45}%`;
  };
  place($("stick-left"), s.lx, s.ly);
  place($("stick-right"), s.rx, s.ry);
}

function logEvent(level, text) {
  const li = document.createElement("li");
  li.className = level;
  const time = document.createElement("time");
  time.textContent = new Date().toLocaleTimeString();
  li.append(time, document.createTextNode(text));
  const list = $("events");
  list.prepend(li);
  while (list.children.length > 200) list.lastChild.remove();
}

function showBanner(text) {
  const b = $("banner");
  b.textContent = text;
  b.hidden = false;
}

// ------------------------------------------------------------------ missions

let missionLayer = null;
let validateTimer = null;

function initMissionLayer() {
  missionLayer = L.layerGroup().addTo(map);
  map.on("click", (e) => {
    if (!state.editing || survey.drawing) return;
    const alt = Number($("ed-alt").value);
    state.editing.waypoints.push({
      lat: e.latlng.lat,
      lon: e.latlng.lng,
      alt: Number.isFinite(alt) ? alt : 30,
      speed: null,
      photo: false,
      gimbal: null,
    });
    renderEditor();
  });
}

function selectedMission() {
  return state.missions.find((m) => m.id === state.selectedMission) || null;
}

function reportText(r) {
  if (!r) return "";
  const minutes = Math.max(1, Math.round(r.duration_s / 60));
  let text = `${r.length_m} m, ~${minutes} min, max alt ${r.max_alt_m} m, `;
  text += r.home_checked ? `${r.max_distance_from_home_m} m from home` : "distance from home not checked (no home point)";
  if (r.errors.length) text += "\n" + r.errors.join("\n");
  return text;
}

function renderMissions() {
  if (!state.missions.some((m) => m.id === state.selectedMission)) {
    state.selectedMission = state.missions.length ? state.missions[0].id : "";
  }
  const sel = $("mission-select");
  const options = state.missions.map((m) => {
    const label = `${m.name} (${m.waypoints.length} wp)${m.report.ok ? "" : " – not valid"}`;
    return new Option(label, m.id);
  });
  sel.replaceChildren(...(options.length ? options : [new Option("(no saved missions)", "")]));
  sel.value = state.selectedMission;
  const m = selectedMission();
  const summary = $("mission-summary");
  summary.textContent = m ? reportText(m.report) : "Create a mission with New.";
  summary.classList.toggle("bad", !!(m && !m.report.ok));
  if (!state.editing) drawMission(m ? toEditable(m) : null, false);
}

$("mission-select").addEventListener("change", (e) => {
  state.selectedMission = e.target.value;
  renderMissions();
});

function toEditable(m) {
  return {
    id: m.id,
    name: m.name,
    speed: m.speed,
    finish_action: m.finish_action,
    waypoints: m.waypoints.map((w) => {
      const gimbal = w.actions.find((a) => a.action === "gimbal_pitch");
      return {
        lat: w.lat,
        lon: w.lon,
        alt: w.alt,
        speed: w.speed,
        photo: w.actions.some((a) => a.action === "photo"),
        gimbal: gimbal ? gimbal.pitch : null,
      };
    }),
  };
}

function toMissionIn(e) {
  return {
    name: e.name,
    speed: e.speed,
    finish_action: e.finish_action,
    waypoints: e.waypoints.map((w) => {
      const actions = [];
      if (w.gimbal !== null) actions.push({ action: "gimbal_pitch", pitch: w.gimbal }); // before the photo
      if (w.photo) actions.push({ action: "photo" });
      const out = { lat: w.lat, lon: w.lon, alt: w.alt, actions };
      if (w.speed !== null) out.speed = w.speed;
      return out;
    }),
  };
}

function drawMission(e, editable) {
  if (!missionLayer) return;
  missionLayer.clearLayers();
  if (!e || !e.waypoints.length) return;
  const pts = e.waypoints.map((w) => [w.lat, w.lon]);
  if (e.finish_action === "first_waypoint") pts.push(pts[0]);
  L.polyline(pts, { color: "#d29922", weight: 3, dashArray: editable ? "6 6" : null }).addTo(missionLayer);
  e.waypoints.forEach((w, i) => {
    const icon = L.divIcon({ className: "", html: `<div class="wp-icon">${i + 1}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] });
    const marker = L.marker([w.lat, w.lon], { icon, draggable: editable }).addTo(missionLayer);
    marker.bindTooltip(`#${i + 1}: ${w.alt} m${w.photo ? ", photo" : ""}${w.gimbal !== null ? `, gimbal ${w.gimbal}°` : ""}`);
    if (editable) {
      marker.on("dragend", () => {
        const ll = marker.getLatLng();
        w.lat = ll.lat;
        w.lon = ll.lng;
        renderEditor();
      });
    }
  });
}

function startEditing(m) {
  if (survey.drawing) setDrawing(false);
  state.editing = m
    ? toEditable(m)
    : { id: null, name: "New mission", speed: 5, finish_action: "go_home", waypoints: [] };
  $("ed-name").value = state.editing.name;
  $("ed-speed").value = String(state.editing.speed);
  $("ed-finish").value = state.editing.finish_action;
  $("editor").hidden = false;
  renderEditor();
}

function stopEditing() {
  state.editing = null;
  $("editor").hidden = true;
  renderMissions();
}

function numberOrNull(input) {
  return input.value === "" || !Number.isFinite(Number(input.value)) ? null : Number(input.value);
}

function renderEditor() {
  const e = state.editing;
  if (!e) return;
  const rows = e.waypoints.map((w, i) => {
    const tr = document.createElement("tr");
    const cell = (child) => {
      const td = document.createElement("td");
      td.append(child);
      tr.append(td);
    };
    const num = (value, onChange, attrs) => {
      const input = document.createElement("input");
      input.type = "number";
      Object.assign(input, attrs);
      input.value = value === null ? "" : String(value);
      input.addEventListener("change", () => {
        onChange(numberOrNull(input));
        renderEditor();
      });
      return input;
    };
    cell(document.createTextNode(String(i + 1)));
    cell(num(w.alt, (v) => (w.alt = v === null ? w.alt : v), { min: 0, max: 1500, step: 1 }));
    cell(num(w.speed, (v) => (w.speed = v), { min: 0.5, max: 15, step: 0.5, placeholder: "–" }));
    const photo = document.createElement("input");
    photo.type = "checkbox";
    photo.checked = w.photo;
    photo.addEventListener("change", () => {
      w.photo = photo.checked;
      renderEditor();
    });
    cell(photo);
    cell(num(w.gimbal, (v) => (w.gimbal = v), { min: -90, max: 35, step: 5, placeholder: "–" }));
    const del = document.createElement("button");
    del.className = "local";
    del.textContent = "×";
    del.title = "Remove waypoint";
    del.addEventListener("click", () => {
      e.waypoints.splice(i, 1);
      renderEditor();
    });
    cell(del);
    return tr;
  });
  $("ed-waypoints").querySelector("tbody").replaceChildren(...rows);
  drawMission(e, true);
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateEditing, 250);
}

async function validateEditing() {
  const e = state.editing;
  const out = $("ed-report");
  if (!e) return;
  if (e.waypoints.length < 2) {
    out.textContent = "Add at least 2 waypoints.";
    out.classList.remove("bad");
    return;
  }
  const r = await op("validate_mission", { mission: toMissionIn(e) });
  if (state.editing !== e) return;
  out.textContent = r.ok ? reportText(r.data) : r.error;
  out.classList.toggle("bad", !r.ok || !r.data.ok);
}

for (const [id, key, parse] of [
  ["ed-name", "name", (v) => v],
  ["ed-speed", "speed", Number],
  ["ed-finish", "finish_action", (v) => v],
]) {
  $(id).addEventListener("change", (ev) => {
    if (!state.editing) return;
    state.editing[key] = parse(ev.target.value);
    renderEditor();
  });
}

$("btn-mission-new").addEventListener("click", () => startEditing(null));
$("btn-mission-edit").addEventListener("click", () => {
  const m = selectedMission();
  if (m) startEditing(m);
});
$("btn-ed-cancel").addEventListener("click", stopEditing);
$("btn-ed-save").addEventListener("click", async () => {
  const e = state.editing;
  if (!e) return;
  const r = await op("save_mission", { id: e.id, mission: toMissionIn(e) });
  if (!r.ok) {
    $("ed-report").textContent = r.error;
    $("ed-report").classList.add("bad");
    return;
  }
  state.selectedMission = r.data.id;
  logEvent("info", `mission "${e.name}" saved`);
  stopEditing();
});
$("btn-mission-delete").addEventListener("click", async () => {
  const m = selectedMission();
  if (!m || !(await confirmDialog(`Delete mission "${m.name}"?`))) return;
  op("delete_mission", { id: m.id });
});
$("btn-mission-upload").addEventListener("click", () => {
  const m = selectedMission();
  if (m) command("mission_upload", { mission_id: m.id });
});

// ------------------------------------------------------------------ scheduled sorties

function serverNow() {
  return Date.now() / 1000 + state.serverOffset;
}

function untilText(seconds) {
  if (seconds < 0) return "now";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h) return `in ${h} h ${m} min`;
  if (m) return `in ${m} min ${s} s`;
  return `in ${s} s`;
}

function renderSchedules() {
  const now = serverNow();
  const rows = state.schedules.map((s) => {
    const li = document.createElement("li");
    li.className = s.state;
    const what = document.createElement("span");
    what.className = "what";
    const when = new Date(s.launch_at * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
    what.textContent = `${when} · ${s.mission_name}`;
    const detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent =
      s.state === "pending" ? `${untilText(s.launch_at - now)}${s.detail ? " · " + s.detail : ""}` : s.detail || "";
    what.append(detail);
    const st = document.createElement("span");
    st.className = "state";
    st.textContent = s.state;
    const btn = document.createElement("button");
    btn.className = "local";
    const active = s.state === "pending" || s.state === "launching";
    btn.textContent = active ? "Cancel" : "Remove";
    btn.addEventListener("click", () => op(active ? "cancel_schedule" : "remove_schedule", { schedule_id: s.id }));
    li.append(what, st, btn);
    return li;
  });
  $("sorties").replaceChildren(...rows);
  updateSortieBanner();
}

function updateSortieBanner() {
  const now = serverNow();
  const warnBefore = state.config ? state.config.schedule.warn_before_s : 60;
  const launching = state.schedules.find((s) => s.state === "launching");
  const soon = state.schedules.find((s) => s.state === "pending" && s.launch_at - now <= warnBefore);
  const s = launching || soon;
  const banner = $("sortie-banner");
  banner.hidden = !s;
  if (!s) return;
  banner.dataset.id = s.id;
  $("sortie-banner-text").textContent = launching
    ? `Sortie "${s.mission_name}" is launching: the aircraft takes off by itself. Pilot at the RC.`
    : `Sortie "${s.mission_name}" takes off by itself ${untilText(s.launch_at - now)}. Pilot to the RC.`;
}

$("sortie-banner-cancel").addEventListener("click", () => {
  const id = $("sortie-banner").dataset.id;
  if (id) op("cancel_schedule", { schedule_id: id });
});

setInterval(() => {
  if (state.schedules.some((s) => s.state === "pending" || s.state === "launching")) renderSchedules();
}, 1000);

function localInputValue(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
$("sortie-time").value = localInputValue(new Date(Date.now() + 15 * 60 * 1000));

$("btn-sortie-create").addEventListener("click", async () => {
  const m = selectedMission();
  if (!m) {
    logEvent("fail", "select a saved mission to schedule");
    return;
  }
  const when = new Date($("sortie-time").value);
  if (Number.isNaN(when.getTime())) {
    logEvent("fail", "set a launch date and time");
    return;
  }
  const text =
    `Schedule "${m.name}" to take off by itself at ${when.toLocaleString()}?\n\n` +
    "At that time the aircraft starts its motors and flies this mission with no further confirmation, " +
    "if the pre-flight checks pass and this page is open.\n\n" +
    `Mission: ${reportText(m.report)}\n\n` +
    "The pilot must be at the RC, ready to take over.";
  if (!(await confirmDialog(text))) return;
  const r = await op("create_schedule", { mission_id: m.id, launch_at: when.getTime() / 1000 }, true);
  if (r.ok) logEvent("warning", `sortie "${m.name}" scheduled for ${when.toLocaleString()}`);
});

// ------------------------------------------------------------------ area survey

let surveyLayer = null;
const survey = { drawing: false, corners: [] }; // corners: [lat, lon]

function initSurveyLayer() {
  surveyLayer = L.layerGroup().addTo(map);
  map.on("click", (e) => {
    if (!survey.drawing) return;
    survey.corners.push([e.latlng.lat, e.latlng.lng]);
    drawSurvey();
  });
}

function areaM2(corners) {
  if (corners.length < 3) return 0;
  const lat0 = corners.reduce((s, c) => s + c[0], 0) / corners.length;
  const k = (Math.PI / 180) * 6371000;
  const pts = corners.map(([lat, lon]) => [lon * k * Math.cos(lat0 * (Math.PI / 180)), lat * k]);
  let s = 0;
  pts.forEach(([x1, y1], i) => {
    const [x2, y2] = pts[(i + 1) % pts.length];
    s += x1 * y2 - x2 * y1;
  });
  return Math.abs(s) / 2;
}

function drawSurvey() {
  if (!surveyLayer) return;
  surveyLayer.clearLayers();
  const c = survey.corners;
  if (c.length >= 3) {
    L.polygon(c, { color: "#a371f7", weight: 2, fillOpacity: 0.12, interactive: false }).addTo(surveyLayer);
  } else if (c.length === 2) {
    L.polyline(c, { color: "#a371f7", weight: 2, interactive: false }).addTo(surveyLayer);
  }
  c.forEach((corner, i) => {
    const icon = L.divIcon({ className: "", html: '<div class="corner-icon"></div>', iconSize: [12, 12], iconAnchor: [6, 6] });
    const marker = L.marker(corner, { icon, draggable: true }).addTo(surveyLayer);
    marker.on("dragend", () => {
      const ll = marker.getLatLng();
      c[i] = [ll.lat, ll.lng];
      drawSurvey();
    });
    marker.on("contextmenu", () => {
      c.splice(i, 1);
      drawSurvey();
    });
  });
  const area = areaM2(c);
  $("sv-report").textContent = c.length
    ? `${c.length} corners${area ? `, about ${area >= 10000 ? (area / 10000).toFixed(2) + " ha" : Math.round(area) + " m²"}` : ""}`
    : "";
  $("sv-report").classList.remove("bad");
}

function setDrawing(on) {
  survey.drawing = on;
  $("btn-survey-draw").textContent = on ? "Finish drawing" : "Draw area";
  $("btn-survey-draw").classList.toggle("active", on);
  map.getContainer().style.cursor = on ? "crosshair" : "";
}

$("btn-survey-draw").addEventListener("click", () => {
  if (!map) return;
  if (!survey.drawing && state.editing) stopEditing(); // one map-click mode at a time
  setDrawing(!survey.drawing);
});

$("btn-survey-clear").addEventListener("click", () => {
  survey.corners = [];
  drawSurvey();
});

$("btn-survey-generate").addEventListener("click", async () => {
  const out = $("sv-report");
  if (survey.corners.length < 3) {
    out.textContent = "Place at least 3 corners on the map.";
    out.classList.add("bad");
    return;
  }
  const opt = (id) => numberOrNull($(id));
  const stamp = new Date().toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  const r = await op("generate_survey", {
    name: `Survey ${stamp}`,
    polygon: survey.corners,
    alt: opt("sv-alt"),
    spacing_m: opt("sv-spacing"),
    angle_deg: opt("sv-angle"),
    speed: opt("sv-speed"),
    photo_spacing_m: opt("sv-photo"),
    gimbal_pitch: -90,
    finish_action: "go_home",
  });
  if (!r.ok) {
    out.textContent = r.error;
    out.classList.add("bad");
    return;
  }
  const d = r.data;
  setDrawing(false);
  out.textContent =
    `${d.lines} lines at ${d.angle_deg}°, ${d.mission.waypoints.length} waypoints` +
    `${d.photos ? `, ${d.photos} photos` : ""}. Review it in the editor, then Save.`;
  out.classList.toggle("bad", !d.report.ok);
  startEditing({ id: null, ...d.mission });
});

renderStatus();
connect();
