/* ECU Telemetry dashboard — single-file, no build step, no external deps. */
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k.nodeType ? k : document.createTextNode(String(k)));
  return e;
};
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
const ago = (iso) => { if (!iso) return "never"; const s = (Date.now() - Date.parse(iso)) / 1000; return s < 60 ? `${s.toFixed(0)}s` : s < 3600 ? `${(s / 60).toFixed(0)}m` : `${(s / 3600).toFixed(1)}h`; };
const tfmt = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "—";
const STATES = ["IDLE", "RUNNING", "DEGRADED", "FAULT"];
const FLAGS = ["OVERTEMP", "OVERCURRENT", "UNDERVOLTAGE", "VIBRATION_HIGH", "SENSOR_STUCK", "GPS_LOST", "COMM_DEGRADED", "ENCODER_FAULT"];
const METRICS = [
  ["rpm", "RPM", 0, "#3ac0ff"], ["temp_c", "TEMP °C", 1, "#ff7a45"], ["current_a", "CURRENT A", 1, "#f5b642"],
  ["voltage_v", "BUS V", 2, "#2fd67a"], ["vibration_g", "VIBRATION g", 2, "#c77dff"], ["speed_kph", "SPEED km/h", 1, "#8b9dff"],
];
const flagsOf = (bits) => FLAGS.filter((_, i) => bits & (1 << i));

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(`${r.status} ${typeof m === "string" ? m : JSON.stringify(m)}`); }
  return r.json();
}
let toastT;
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); clearTimeout(toastT);
  toastT = setTimeout(() => t.classList.add("hidden"), err ? 5000 : 2500);
}

/* ---------------------------------------------------------------- state */
const S = {
  devices: new Map(),          // id -> device summary (from /api/devices, patched live)
  live: new Map(),             // id -> {metric: [[ts, v], ...]} ring buffers for sparklines + 5m live charts
  alerts: new Map(),           // id -> alert (open/acked)
  stats: {},
  view: null,                  // current view controller
  sse: null, sseOk: false,
};
const RING = 600; // ~5 min at 2 Hz
function pushLive(t) {
  let buf = S.live.get(t.device_id);
  if (!buf) { buf = { pos: [] }; for (const [m] of METRICS) buf[m] = []; S.live.set(t.device_id, buf); }
  const ts = Date.parse(t.ts_iso);
  for (const [m] of METRICS) { buf[m].push([ts, t[m]]); if (buf[m].length > RING) buf[m].shift(); }
  if (t.lat || t.lon) { buf.pos.push([t.lat, t.lon]); if (buf.pos.length > 2000) buf.pos.shift(); }
}

/* ---------------------------------------------------------------- canvas helpers */
function setupCanvas(c) {
  const r = c.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  const w = Math.max(10, r.width), hh = Math.max(10, r.height);
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(hh * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(hh * dpr); }
  const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, hh);
  return [ctx, w, hh];
}
function sparkline(c, pts, color) {
  const [ctx, w, hh] = setupCanvas(c);
  if (pts.length < 2) return;
  let mn = Infinity, mx = -Infinity; for (const [, v] of pts) { if (v < mn) mn = v; if (v > mx) mx = v; }
  if (mx - mn < 1e-9) { mn -= 1; mx += 1; }
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0] || x0 + 1;
  ctx.beginPath();
  pts.forEach(([t, v], i) => { const x = ((t - x0) / (x1 - x0 || 1)) * (w - 2) + 1, y = hh - 2 - ((v - mn) / (mx - mn)) * (hh - 4); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.stroke();
  ctx.lineTo(w - 1, hh); ctx.lineTo(1, hh); ctx.closePath(); ctx.fillStyle = color + "22"; ctx.fill();
}
function lineChart(c, series, opts) {
  // series: [{pts:[[ts,v]], color, band?:[[ts,min,max]]}] ; opts: {t0,t1, limit?, unit, decimals}
  const [ctx, w, hh] = setupCanvas(c);
  const padL = 44, padB = 16, padT = 6, padR = 6;
  const pw = w - padL - padR, ph = hh - padT - padB;
  let mn = Infinity, mx = -Infinity;
  for (const s of series) { for (const [, v] of s.pts) { if (v < mn) mn = v; if (v > mx) mx = v; } if (s.band) for (const [, a, b] of s.band) { if (a < mn) mn = a; if (b > mx) mx = b; } }
  if (opts.limit !== undefined && opts.limit !== null && isFinite(opts.limit)) { mn = Math.min(mn, opts.limit); mx = Math.max(mx, opts.limit); }
  if (!isFinite(mn)) { ctx.fillStyle = "#4b5563"; ctx.fillText("no data", padL + 8, hh / 2); return; }
  if (mx - mn < 1e-9) { mn -= 1; mx += 1; } const pad = (mx - mn) * 0.08; mn -= pad; mx += pad;
  const t0 = opts.t0, t1 = opts.t1;
  const X = (t) => padL + ((t - t0) / (t1 - t0 || 1)) * pw, Y = (v) => padT + (1 - (v - mn) / (mx - mn)) * ph;
  ctx.strokeStyle = "#1f2937"; ctx.fillStyle = "#7d8896"; ctx.font = "10px ui-monospace,monospace"; ctx.textAlign = "right"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const v = mn + (mx - mn) * i / 4, y = Y(v); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke(); ctx.fillText(fmt(v, opts.decimals), padL - 4, y + 3); }
  ctx.textAlign = "center";
  for (let i = 0; i <= 4; i++) { const t = t0 + (t1 - t0) * i / 4; ctx.fillText(new Date(t).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: (t1 - t0) < 900e3 ? "2-digit" : undefined }), X(t), hh - 3); }
  if (opts.limit !== undefined && opts.limit !== null && isFinite(opts.limit)) { ctx.setLineDash([4, 4]); ctx.strokeStyle = "#ff4d5e88"; ctx.beginPath(); ctx.moveTo(padL, Y(opts.limit)); ctx.lineTo(w - padR, Y(opts.limit)); ctx.stroke(); ctx.setLineDash([]); }
  for (const s of series) {
    if (s.band && s.band.length > 1) { ctx.beginPath(); s.band.forEach(([t, a], i) => i ? ctx.lineTo(X(t), Y(a)) : ctx.moveTo(X(t), Y(a))); for (let i = s.band.length - 1; i >= 0; i--) ctx.lineTo(X(s.band[i][0]), Y(s.band[i][2])); ctx.closePath(); ctx.fillStyle = s.color + "22"; ctx.fill(); }
    if (s.pts.length) { ctx.beginPath(); s.pts.forEach(([t, v], i) => i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))); ctx.strokeStyle = s.color; ctx.lineWidth = 1.4; ctx.stroke(); }
  }
}
function drawMap(c, pos, faults) {
  const [ctx, w, hh] = setupCanvas(c);
  ctx.strokeStyle = "#1a2230"; for (let x = 0; x < w; x += 20) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hh); ctx.stroke(); } for (let y = 0; y < hh; y += 20) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  const pts = pos.filter(([a, b]) => a || b);
  if (pts.length < 2) { ctx.fillStyle = "#4b5563"; ctx.font = "11px ui-monospace,monospace"; ctx.fillText(faults?.includes("GPS_LOST") ? "GPS LOST" : "no position", 10, hh / 2); return; }
  let la0 = Infinity, la1 = -Infinity, lo0 = Infinity, lo1 = -Infinity;
  for (const [la, lo] of pts) { la0 = Math.min(la0, la); la1 = Math.max(la1, la); lo0 = Math.min(lo0, lo); lo1 = Math.max(lo1, lo); }
  const cosL = Math.cos(((la0 + la1) / 2) * Math.PI / 180) || 1;
  const spanY = Math.max(la1 - la0, 1e-5), spanX = Math.max((lo1 - lo0) * cosL, 1e-5), sc = Math.min((w - 20) / spanX, (hh - 20) / spanY);
  const X = (lo) => 10 + ((lo - lo0) * cosL) * sc + ((w - 20) - spanX * sc) / 2, Y = (la) => hh - 10 - (la - la0) * sc - ((hh - 20) - spanY * sc) / 2;
  ctx.beginPath(); pts.forEach(([la, lo], i) => i ? ctx.lineTo(X(lo), Y(la)) : ctx.moveTo(X(lo), Y(la)));
  ctx.strokeStyle = "#3ac0ff"; ctx.lineWidth = 1.5; ctx.stroke();
  const [la, lo] = pts[pts.length - 1]; ctx.beginPath(); ctx.arc(X(lo), Y(la), 4, 0, 7); ctx.fillStyle = "#2fd67a"; ctx.fill();
  ctx.fillStyle = "#7d8896"; ctx.font = "10px ui-monospace,monospace"; ctx.fillText(`${la.toFixed(5)}, ${lo.toFixed(5)}`, 6, hh - 4);
}

/* ---------------------------------------------------------------- top bar */
function renderKpis() {
  const s = S.stats, devs = [...S.devices.values()], online = devs.filter(d => d.online).length;
  const crit = [...S.alerts.values()].filter(a => a.severity === "critical").length;
  $("#kpis").replaceChildren(
    kpi("Devices", `${online}/${devs.length}`, online === devs.length && devs.length ? "ok" : devs.length ? "warn" : ""),
    kpi("Frames", (s.frames ?? 0).toLocaleString()),
    kpi("Bad CRC", s.bad_crc ?? 0, s.bad_crc ? "warn" : ""),
    kpi("Loss", `${fmt(s.loss_pct)}%`, s.loss_pct > 10 ? "crit" : s.loss_pct > 2 ? "warn" : ""),
    kpi("Open alerts", S.alerts.size, crit ? "crit" : S.alerts.size ? "warn" : "ok"),
    kpi("Uptime", s.uptime_s ? `${Math.floor(s.uptime_s / 60)}m` : "—"),
  );
  const b = $("#alerts-badge"); b.textContent = S.alerts.size; b.className = "badge" + (S.alerts.size ? "" : " zero");
}
const kpi = (l, v, cls = "") => h("div", { class: "kpi" }, h("span", { class: "l" }, l), h("span", { class: `v ${cls}` }, v));

/* ---------------------------------------------------------------- alerts drawer */
function renderDrawer() {
  const list = [...S.alerts.values()].sort((a, b) => (a.severity === "critical" ? 0 : 1) - (b.severity === "critical" ? 0 : 1) || b.id - a.id);
  $("#drawer-list").replaceChildren(...(list.length ? list.map(a => h("div", { class: `al ${a.severity} ${a.status}` },
    h("span", { class: "r" }, a.rule, " ", h("span", { class: `sev ${a.severity}` }, a.severity)),
    a.status === "open" ? h("button", { class: "btn small", onclick: () => ack(a.id) }, "ack") : h("span", { class: "muted" }, "acked"),
    h("span", { class: "d", onclick: () => { location.hash = `#/device/${a.device_id}`; } }, a.device_id, " · ", ago(a.opened_at_iso), " ago"),
    h("span", { class: "m" }, a.message || ""),
  )) : [h("div", { class: "empty" }, "no open alerts")]));
}
async function ack(id) { try { await api(`/api/alerts/${id}/ack`, { method: "POST" }); } catch (e) { toast(e.message, true); } }

/* ---------------------------------------------------------------- fleet view */
function FleetView() {
  const root = $("#view"); const tiles = new Map();
  root.replaceChildren(h("h2", {}, "Fleet"), h("div", { class: "grid", id: "grid" }));
  const grid = $("#grid");
  function tile(d) {
    const el = h("div", { class: "tile", onclick: () => { location.hash = `#/device/${d.device_id}`; } });
    grid.append(el); tiles.set(d.device_id, el); return el;
  }
  function draw(d) {
    const el = tiles.get(d.device_id) || tile(d); const L = d.latest || {}, buf = S.live.get(d.device_id);
    el.className = `tile st${d.state ?? 0}${d.online ? "" : " offline"}`;
    const nAl = d.open_alerts || [...S.alerts.values()].filter(a => a.device_id === d.device_id).length;
    el.replaceChildren(
      h("div", { class: "row" }, h("span", { class: `dot ${d.online ? "on" : ""}` }), h("span", { class: "id" }, d.device_id),
        h("span", { class: `pill ${d.state === 3 ? "crit" : d.state === 2 ? "warn" : d.state === 1 ? "ok" : "dim"}` }, STATES[d.state ?? 0]),
        h("span", { class: "meta" }, `fw ${d.fw_version || "?"}`, h("br"), d.hw_model || "")),
      h("div", { class: "vals" },
        val("rpm", fmt(L.rpm, 0)), val("temp", `${fmt(L.temp_c)} °C`), val("curr", `${fmt(L.current_a)} A`), val("bus", `${fmt(L.voltage_v)} V`),
        val("vib", `${fmt(L.vibration_g, 2)} g`), val("spd", `${fmt(L.speed_kph)} km/h`)),
      h("canvas", { class: "sp1" }), h("canvas", { class: "sp2" }),
      h("div", { class: "foot" }, h("span", { class: `pill ${d.loss_pct > 10 ? "crit" : d.loss_pct > 2 ? "warn" : "dim"}` }, `loss ${fmt(d.loss_pct)}%`),
        h("span", { class: `pill ${nAl ? (d.faults?.length ? "crit" : "warn") : "dim"}` }, `${nAl} alert${nAl === 1 ? "" : "s"}`),
        ...(d.faults || []).slice(0, 2).map(f => h("span", { class: "pill crit" }, f)),
        h("span", { style: "margin-left:auto" }, d.online ? `seen ${ago(d.last_seen)} ago` : `OFFLINE · ${ago(d.last_seen)}`)),
    );
    if (buf) { sparkline($(".sp1", el), buf.rpm, "#3ac0ff"); sparkline($(".sp2", el), buf.temp_c, "#ff7a45"); }
  }
  const val = (k, v) => h("div", { class: "val" }, h("span", { class: "k" }, k), h("span", {}, v));
  function all() {
    const devs = [...S.devices.values()].sort((a, b) => a.device_id.localeCompare(b.device_id));
    if (!devs.length) { grid.replaceChildren(h("div", { class: "empty" }, "no devices yet — start an agent: firmware/build/ecu_agent --id ECU-0001")); return; }
    if (grid.querySelector(".empty")) grid.replaceChildren();
    devs.forEach(draw);
  }
  all();
  return { onTelemetry: (t) => { const d = S.devices.get(t.device_id); if (d) draw(d); }, onDevice: all, onAlert: all, onResize: all, tick: all };
}

/* ---------------------------------------------------------------- device view */
function DeviceView(id) {
  const root = $("#view");
  let detail = null, range = "5m", hist = null, tab = "alerts", tabState = {};
  const canvases = {};
  root.replaceChildren(
    h("div", { class: "dev-head", id: "dh" }),
    h("div", { class: "dev-layout" },
      h("div", {}, h("div", { class: "charts", id: "charts" }, ...METRICS.map(([m, label]) => h("div", { class: "chart" }, h("div", { class: "ch" }, h("span", {}, label), h("b", { id: `cur-${m}` }, "—")), h("canvas", { id: `c-${m}` })))),
        h("div", { class: "tabs", id: "tabs" }), h("div", { class: "tabbody", id: "tabbody" })),
      h("div", { class: "side" },
        h("div", { class: "panel" }, h("h3", {}, "Position"), h("canvas", { id: "map" })),
        h("div", { class: "panel" }, h("h3", {}, "Link"), h("div", { class: "kv", id: "link" })),
        h("div", { class: "panel" }, h("h3", {}, "Device"), h("div", { class: "kv", id: "meta" })))),
  );
  for (const [m] of METRICS) canvases[m] = $(`#c-${m}`);
  const TABS = [["alerts", "Alerts"], ["logs", "Logs"], ["config", "Config"], ["firmware", "Firmware"], ["fault", "Fault injection"], ["diagnose", "Diagnose"]];

  async function load() {
    try { detail = await api(`/api/devices/${id}`); } catch (e) { root.replaceChildren(h("div", { class: "empty" }, e.message)); return; }
    S.devices.set(id, detail);
    head(); side(); await loadHistory(); drawCharts(); renderTabs(); renderTab();
  }
  function head() {
    const d = detail, L = d.latest || {};
    $("#dh").replaceChildren(
      h("button", { class: "btn small", onclick: () => { location.hash = "#/"; } }, "← fleet"),
      h("span", { class: `dot ${d.online ? "on" : ""}` }), h("span", { class: "id" }, d.device_id),
      h("span", { class: `pill ${d.state === 3 ? "crit" : d.state === 2 ? "warn" : d.state === 1 ? "ok" : "dim"}` }, STATES[d.state ?? 0]),
      h("span", { class: "sub" }, `${d.hw_model || "?"} · fw ${d.fw_version || "?"} · cfg v${d.config_version} · ${d.online ? "online" : "OFFLINE"} · tcp ${d.tcp_connected ? "up" : "down"}`),
      h("div", { class: "chips", id: "chips" }, ...(flagsOf(L.fault_flags || 0).length ? flagsOf(L.fault_flags || 0).map(f => h("span", { class: "chip" }, f)) : [h("span", { class: "chip none" }, "no faults")])),
      h("div", { class: "range" }, ...["5m", "1h", "24h"].map(r => h("button", { class: `btn small ${r === range ? "active" : ""}`, onclick: async () => { range = r; await loadHistory(); drawCharts(); head(); } }, r))),
    );
  }
  function side() {
    const d = detail, s = d.stats || {};
    const kv = (o) => Object.entries(o).flatMap(([k, v]) => [h("span", { class: "k" }, k), h("span", { class: "v" }, v)]);
    $("#link").replaceChildren(...kv({ "loss %": fmt(s.loss_pct), "frames rx": s.frames_rx ?? 0, "seq gaps": s.seq_gaps ?? 0, "bad crc": s.bad_crc ?? 0, "reconnects (backend)": s.reconnects ?? 0, "reconnects (device)": d.device_reconnects ?? "—", "last seen": `${ago(d.last_seen)} ago`, "last seq": s.last_seq ?? "—" }));
    $("#meta").replaceChildren(...kv({ "hw": d.hw_model || "—", "fw": d.fw_version || "—", "uptime": d.uptime_s != null ? `${Math.floor(d.uptime_s / 60)}m ${d.uptime_s % 60}s` : "—", "hz": d.telemetry_hz ?? "—", "cfg (backend)": `v${d.config_version}`, "cfg (device)": `v${d.reported_config_version}`, "first seen": d.first_seen ? new Date(d.first_seen).toLocaleString() : "—", "open alerts": d.open_alerts }));
    const buf = S.live.get(id);
    drawMap($("#map"), buf?.pos?.length ? buf.pos : (hist?.rows || []).map(r => [r.lat, r.lon]), d.faults);
  }
  async function loadHistory() {
    const secs = { "5m": 300, "1h": 3600, "24h": 86400 }[range], step = range === "5m" ? "raw" : range === "1h" ? "10s" : "1m";
    const since = new Date(Date.now() - secs * 1000).toISOString();
    try { hist = await api(`/api/devices/${id}/telemetry?since=${since}&step=${step}&limit=5000`); } catch (e) { hist = { rows: [] }; }
    if (range === "5m" && hist.rows.length) {   // seed live buffers so charts continue seamlessly
      const buf = { pos: [] }; for (const [m] of METRICS) buf[m] = hist.rows.map(r => [r.ts * 1000, r[m]]).slice(-RING);
      buf.pos = hist.rows.filter(r => r.lat || r.lon).map(r => [r.lat, r.lon]); S.live.set(id, buf);
    }
  }
  function drawCharts() {
    const now = Date.now(), secs = { "5m": 300, "1h": 3600, "24h": 86400 }[range], t0 = now - secs * 1000;
    const cfg = detail?.config || {}, limits = { rpm: cfg.rpm_limit, temp_c: cfg.temp_limit_c, current_a: cfg.current_limit_a, voltage_v: 80 };
    const buf = S.live.get(id);
    for (const [m, , dec, color] of METRICS) {
      let pts, band;
      if (range === "5m") pts = (buf?.[m] || []).filter(([t]) => t >= t0);
      else { pts = (hist?.rows || []).map(r => [r.ts * 1000, r[m]]); band = (hist?.rows || []).filter(r => r[`${m}_min`] != null).map(r => [r.ts * 1000, r[`${m}_min`], r[`${m}_max`]]); }
      lineChart(canvases[m], [{ pts, color, band }], { t0, t1: now, limit: limits[m], decimals: dec });
      const last = pts.length ? pts[pts.length - 1][1] : detail?.latest?.[m]; $(`#cur-${m}`).textContent = fmt(last, dec);
    }
  }
  function renderTabs() {
    const nAl = [...S.alerts.values()].filter(a => a.device_id === id).length;
    $("#tabs").replaceChildren(...TABS.map(([k, l]) => h("div", { class: `tab ${k === tab ? "active" : ""}`, onclick: () => { tab = k; renderTabs(); renderTab(); } }, l, k === "alerts" && nAl ? h("span", { class: "n" }, nAl) : null)));
  }
  async function renderTab() {
    const body = $("#tabbody"); body.replaceChildren(h("span", { class: "muted" }, "loading…"));
    try { await ({ alerts: tabAlerts, logs: tabLogs, config: tabConfig, firmware: tabFirmware, fault: tabFault, diagnose: tabDiagnose })[tab](body); }
    catch (e) { body.replaceChildren(h("div", { class: "empty" }, e.message)); }
  }
  async function tabAlerts(body) {
    const rows = await api(`/api/alerts?device=${id}&status=all&limit=100`);
    body.replaceChildren(rows.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["sev", "rule", "metric", "value", "status", "opened", "resolved", "message", ""].map(x => h("th", {}, x)))),
      h("tbody", {}, ...rows.map(a => h("tr", { class: a.severity === "critical" ? "crit" : a.severity },
        h("td", {}, h("span", { class: `sev ${a.severity}` }, a.severity)), h("td", {}, a.rule), h("td", {}, a.metric || ""), h("td", {}, a.value != null ? fmt(a.value, 2) : ""),
        h("td", {}, a.status), h("td", {}, tfmt(a.opened_at_iso)), h("td", {}, tfmt(a.resolved_at_iso)), h("td", { class: "muted" }, a.message || ""),
        h("td", {}, a.status === "open" ? h("button", { class: "btn small", onclick: async () => { await ack(a.id); renderTab(); } }, "ack") : ""))))) : h("div", { class: "empty" }, "no alerts for this device"));
  }
  async function tabLogs(body) {
    const rows = await api(`/api/devices/${id}/logs?limit=300`);
    const list = h("div", { class: "loglist", id: "loglist" }, ...rows.map(logLine));
    body.replaceChildren(h("div", { class: "actions", style: "margin:0 0 8px" }, h("span", { class: "muted" }, `${rows.length} entries · live`), h("button", { class: "btn small", onclick: () => renderTab() }, "refresh")), list);
  }
  const logLine = (l) => h("div", { class: "log" }, h("span", { class: "t" }, tfmt(l.ts_iso), " "), h("span", { class: `l${l.level}` }, `[${(l.level_name || ["debug", "info", "warn", "error"][l.level] || l.level).padEnd(5)}] `), l.message);
  async function tabConfig(body) {
    const c = await api(`/api/devices/${id}/config`);
    const f = (name, label, val, type = "number", step = "any") => h("label", {}, label, h("input", { name, type, step, value: val ?? "" }));
    const form = h("div", { class: "form" },
      f("telemetry_hz", "telemetry hz", c.telemetry_hz ?? detail.telemetry_hz ?? 10, "number", "1"),
      h("label", {}, "log level", h("select", { name: "log_level" }, ...["debug", "info", "warn", "error"].map((n, i) => h("option", { value: i, selected: (c.log_level ?? 1) === i ? "" : null }, n)))),
      f("rpm_limit", "rpm limit", c.rpm_limit ?? 6000), f("temp_limit_c", "temp limit °C", c.temp_limit_c ?? 90), f("current_limit_a", "current limit A", c.current_limit_a ?? 120));
    const status = h("div", { class: "status" }, configStatus(c));
    tabState.configStatus = status;
    body.replaceChildren(form, h("div", { class: "actions" },
      h("button", { class: "btn primary", onclick: async (ev) => {
        const b = {}; form.querySelectorAll("input,select").forEach(i => b[i.name] = i.name === "log_level" ? +i.value : +i.value);
        ev.target.disabled = true;
        try { const r = await api(`/api/devices/${id}/config`, { method: "PUT", body: b }); status.textContent = configStatus(r); toast(`config v${r.version} ${r.pushed ? "pushed" : "queued (device offline)"}`); }
        catch (e) { toast(e.message, true); } ev.target.disabled = false; } }, "push CONFIG_SET"),
      status),
      h("h3", { style: "margin-top:16px" }, "history"),
      h("table", {}, h("thead", {}, h("tr", {}, ...["v", "hz", "log", "rpm", "temp", "curr", "ack", "at"].map(x => h("th", {}, x)))),
        h("tbody", {}, ...(detail.config_history || []).map(r => h("tr", {}, h("td", {}, r.version), h("td", {}, r.telemetry_hz), h("td", {}, ["debug", "info", "warn", "error"][r.log_level]), h("td", {}, r.rpm_limit), h("td", {}, r.temp_limit_c), h("td", {}, r.current_limit_a), h("td", {}, ackPill(r.ack_status)), h("td", {}, tfmt(new Date(r.created_at * 1000).toISOString())))))));
  }
  const configStatus = (c) => c.version ? `backend v${c.version} · ack: ${c.ack_status || "—"} · device reports v${c.reported_config_version}` : `no backend config yet · device reports v${c.reported_config_version}`;
  const ackPill = (s) => h("span", { class: `pill ${s === "applied" ? "ok" : s === "rejected" ? "crit" : "warn"}` }, s || "—");
  async function tabFirmware(body) {
    const [reg, job] = await Promise.all([api("/api/firmware"), api(`/api/devices/${id}/firmware/status`)]);
    const nv = h("input", { placeholder: "e.g. 1.1.0", style: "width:110px" }), nn = h("input", { placeholder: "notes", style: "width:220px" }), ns = h("input", { type: "number", value: 1500000, style: "width:120px" });
    body.replaceChildren(
      h("h3", {}, `installed: fw ${detail.fw_version || "?"}`),
      h("table", {}, h("thead", {}, h("tr", {}, ...["version", "notes", "size", "crc", "added", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, ...reg.map(f => h("tr", {}, h("td", {}, h("b", {}, f.version)), h("td", { class: "muted" }, f.notes || ""), h("td", {}, `${(f.size_bytes / 1e6).toFixed(2)} MB`), h("td", {}, "0x" + (f.crc >>> 0).toString(16)), h("td", {}, tfmt(f.created_at_iso)),
          h("td", {}, h("button", { class: `btn small ${f.version === detail.fw_version ? "" : "primary"}`, disabled: f.version === detail.fw_version ? "" : null, onclick: async () => { try { const r = await api(`/api/devices/${id}/firmware`, { method: "POST", body: { version: f.version } }); toast(`OTA job #${r.job_id} ${r.pushed ? "pushed" : "queued"}`); renderTab(); } catch (e) { toast(e.message, true); } } }, "deploy")))),
          reg.length ? null : h("tr", {}, h("td", { colspan: 6, class: "muted" }, "registry empty")))),
      h("div", { class: "actions" }, h("span", { class: "muted" }, "register:"), nv, nn, ns, h("button", { class: "btn", onclick: async () => { try { await api("/api/firmware", { method: "POST", body: { version: nv.value, notes: nn.value, size_bytes: +ns.value } }); toast("firmware registered"); renderTab(); } catch (e) { toast(e.message, true); } } }, "add")),
      h("h3", { style: "margin-top:16px" }, "OTA job"), h("div", { id: "job" }, jobView(job)));
    tabState.jobEl = $("#job");
  }
  function jobView(job) {
    if (!job || job.status === "none") return h("span", { class: "muted" }, "no job");
    const steps = ["pending", "sent", "accepted", "downloading", "applied", "complete"], idx = steps.indexOf(job.status), bad = ["rejected", "failed", "queued"].includes(job.status);
    return h("div", {}, h("div", { class: "muted" }, `#${job.id} ${job.from_version || "?"} → ${job.target_version} · ${job.status} · ${ago(job.updated_at_iso)} ago`),
      h("div", { class: "job" }, ...steps.map((s, i) => h("span", { class: `step ${i < idx ? "done" : i === idx ? "cur" : ""}` }, s)), bad ? h("span", { class: "step bad" }, job.status) : null),
      h("div", { class: "muted", style: "margin-top:6px;font-size:11px" }, job.history.map(x => `${tfmt(x.ts)} ${x.status}`).join("  →  ")));
  }
  async function tabFault(body) {
    const boxes = FLAGS.map(f => { const cb = h("input", { type: "checkbox", value: f }); const lab = h("label", {}, cb, f); cb.addEventListener("change", () => lab.classList.toggle("on", cb.checked)); return lab; });
    const dur = h("input", { type: "number", value: 10000, step: 1000, style: "width:120px" });
    const sel = () => boxes.filter(l => l.firstChild.checked).map(l => l.firstChild.value);
    const send = async (set, clear) => { try { const r = await api(`/api/devices/${id}/fault`, { method: "POST", body: { set, clear, duration_ms: +dur.value } }); toast(`FAULT_INJECT pushed set=${r.set.join("|") || "-"} clear=${r.clear.join("|") || "-"}`); setTimeout(load, 500); } catch (e) { toast(e.message, true); } };
    body.replaceChildren(h("div", { class: "flags" }, ...boxes),
      h("div", { class: "actions" }, h("label", { style: "flex-direction:row;align-items:center;gap:6px" }, "duration ms (0 = until cleared)", dur),
        h("button", { class: "btn danger", onclick: () => send(sel(), []) }, "inject selected"),
        h("button", { class: "btn", onclick: () => send([], sel()) }, "clear selected"),
        h("button", { class: "btn", onclick: async () => { try { await api(`/api/devices/${id}/cmd`, { method: "POST", body: { cmd: "clear_faults" } }); toast("CMD clear_faults pushed"); } catch (e) { toast(e.message, true); } } }, "CMD clear_faults"),
        h("button", { class: "btn", onclick: async () => { try { await api(`/api/devices/${id}/cmd`, { method: "POST", body: { cmd: "reboot" } }); toast("CMD reboot pushed"); } catch (e) { toast(e.message, true); } } }, "CMD reboot")),
      h("h3", { style: "margin-top:16px" }, "recent injections"),
      h("table", {}, h("thead", {}, h("tr", {}, ...["at", "set", "clear", "duration", "device ack"].map(x => h("th", {}, x)))),
        h("tbody", {}, ...(detail.fault_injections || []).map(f => h("tr", {}, h("td", {}, tfmt(f.created_at_iso)), h("td", {}, flagsOf(f.set_flags).join(" ") || "—"), h("td", {}, flagsOf(f.clear_flags).join(" ") || "—"), h("td", {}, `${f.duration_ms} ms`), h("td", {}, f.ack_flags != null ? (flagsOf(f.ack_flags).join(" ") || "none active") : h("span", { class: "muted" }, "pending")))))));
  }
  async function tabDiagnose(body) {
    const out = h("div", { class: "diag" }, ...(detail.diagnoses?.length ? [diagView(detail.diagnoses[0])] : [h("span", { class: "muted" }, "no diagnosis yet")]));
    body.replaceChildren(h("div", { class: "actions", style: "margin:0 0 10px" },
      h("button", { class: "btn primary", onclick: async (ev) => { ev.target.disabled = true; out.replaceChildren(h("span", { class: "muted" }, "analysing…")); try { const d = await api(`/api/devices/${id}/diagnose`, { method: "POST" }); out.replaceChildren(diagView(d)); detail.diagnoses = [d, ...(detail.diagnoses || [])]; } catch (e) { toast(e.message, true); out.replaceChildren(h("div", { class: "empty" }, e.message)); } ev.target.disabled = false; } }, "run diagnosis"),
      h("span", { class: "muted" }, "uses Claude when ANTHROPIC_API_KEY is set, otherwise the built-in heuristic diagnostician")), out);
  }
  function diagView(d) {
    return h("div", {}, h("div", { class: "actions", style: "margin:0" }, h("span", { class: `pill ${d.model === "heuristic" ? "warn" : "info"}` }, `model: ${d.model}`), h("span", { class: "pill dim" }, `confidence ${(d.confidence * 100).toFixed(0)}%`), h("span", { class: "muted" }, tfmt(d.created_at))),
      h("div", { class: "summary" }, d.summary),
      h("h3", {}, "probable causes"), ...(d.probable_causes || []).map(c => h("div", { class: "cause" }, h("div", {}, h("div", { class: "lik" }, `${(c.likelihood * 100).toFixed(0)}%`), h("div", { class: "bar" }, h("i", { style: `width:${c.likelihood * 100}%` }))), h("div", {}, h("div", {}, c.cause), h("div", { class: "ev" }, c.evidence)))),
      h("h3", { style: "margin-top:12px" }, "recommended actions"), h("ul", {}, ...(d.recommended_actions || []).map(a => h("li", {}, a))));
  }
  load();
  return {
    onTelemetry: (t) => { if (t.device_id !== id || !detail) return; detail.latest = t; detail.state = t.state; detail.faults = t.faults; detail.stats = { ...detail.stats, loss_pct: t.loss_pct }; if (range === "5m") drawCharts(); else for (const [m, , dec] of METRICS) $(`#cur-${m}`).textContent = fmt(t[m], dec); head(); side(); },
    onDevice: (e) => { if (e.device_id !== id) return; if (e.event === "fw_job" && tab === "firmware" && tabState.jobEl) tabState.jobEl.replaceChildren(jobView(e.job)); if (e.event === "config_ack" && tab === "config") renderTab(); if (["hello", "online", "offline", "tcp_connected", "tcp_disconnected", "fault_ack", "config_ack", "fw_job"].includes(e.event)) api(`/api/devices/${id}`).then(d => { detail = { ...detail, ...d }; S.devices.set(id, detail); head(); side(); }).catch(() => {}); },
    onAlert: (a) => { if (a.device_id !== id) return; renderTabs(); if (tab === "alerts") renderTab(); },
    onLog: (l) => { if (l.device_id !== id || tab !== "logs") return; const ll = $("#loglist"); if (ll) { ll.prepend(logLine(l)); while (ll.children.length > 300) ll.lastChild.remove(); } },
    onResize: () => { drawCharts(); side(); }, tick: () => { if (range !== "5m") return; drawCharts(); },
  };
}

/* ---------------------------------------------------------------- SSE + polling */
function connectSSE() {
  if (S.sse) S.sse.close();
  const es = new EventSource("/api/events/stream"); S.sse = es;
  es.onopen = () => { S.sseOk = true; $("#sse-dot").classList.add("on"); };
  es.onerror = () => { S.sseOk = false; $("#sse-dot").classList.remove("on"); };
  es.addEventListener("telemetry", (e) => {
    const t = JSON.parse(e.data); pushLive(t);
    const d = S.devices.get(t.device_id);
    if (d) { d.latest = t; d.state = t.state; d.faults = t.faults; d.loss_pct = t.loss_pct; d.online = true; d.last_seen = t.ts_iso; }
    else { S.devices.set(t.device_id, { device_id: t.device_id, latest: t, state: t.state, faults: t.faults, loss_pct: t.loss_pct, online: true, last_seen: t.ts_iso, fw_version: t.fw_version, open_alerts: 0 }); S.view?.onDevice?.({ device_id: t.device_id, event: "new" }); }
    S.view?.onTelemetry?.(t);
  });
  es.addEventListener("alert", (e) => {
    const a = JSON.parse(e.data);
    if (a.status === "resolved") S.alerts.delete(a.id); else S.alerts.set(a.id, a);
    const d = S.devices.get(a.device_id); if (d) d.open_alerts = [...S.alerts.values()].filter(x => x.device_id === a.device_id).length;
    if (a.action === "opened") toast(`${a.severity.toUpperCase()} ${a.device_id} ${a.rule}`, a.severity === "critical");
    renderKpis(); renderDrawer(); S.view?.onAlert?.(a);
  });
  es.addEventListener("device", (e) => {
    const ev = JSON.parse(e.data); const d = S.devices.get(ev.device_id);
    if (d) { if (ev.event === "online") d.online = true; if (ev.event === "offline") d.online = false; if (ev.event === "tcp_connected") d.tcp_connected = true; if (ev.event === "tcp_disconnected") d.tcp_connected = false; if (ev.fw_version) d.fw_version = ev.fw_version; if (ev.hw_model) d.hw_model = ev.hw_model; }
    else if (ev.event === "hello" || ev.event === "online") refreshDevices();
    renderKpis(); S.view?.onDevice?.(ev);
  });
  es.addEventListener("log", (e) => S.view?.onLog?.(JSON.parse(e.data)));
}
async function refreshDevices() {
  try {
    const [devs, alerts, stats] = await Promise.all([api("/api/devices"), api("/api/alerts?status=open&limit=500"), api("/api/stats")]);
    for (const d of devs) { const old = S.devices.get(d.device_id); S.devices.set(d.device_id, old ? { ...old, ...d, latest: d.latest || old.latest } : d); }
    S.alerts = new Map(alerts.map(a => [a.id, a])); S.stats = stats;
    renderKpis(); renderDrawer(); S.view?.onDevice?.({ event: "refresh" });
  } catch (e) { /* server down: SSE dot shows it */ }
}

/* ---------------------------------------------------------------- router + boot */
function route() {
  const m = location.hash.match(/^#\/device\/([^/]+)/);
  S.view = m ? DeviceView(decodeURIComponent(m[1])) : FleetView();
}
$("#alerts-btn").onclick = () => $("#drawer").classList.toggle("hidden");
$("#drawer-close").onclick = () => $("#drawer").classList.add("hidden");
window.addEventListener("hashchange", route);
window.addEventListener("resize", () => S.view?.onResize?.());
setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour12: false }); }, 1000);
setInterval(refreshDevices, 10000);
setInterval(() => S.view?.tick?.(), 2000);
refreshDevices().then(route);
connectSSE();
})();
