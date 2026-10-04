/* Local Network Monitor — dashboard logic.
 *
 * Plain ES2020. No framework, no CDN, no build step: the page has to work on a
 * machine that may have no internet access, and the repository has no npm
 * dependencies to babysit.
 *
 * The UI is deliberately structured as a story, and this file mirrors it:
 *
 *   1. right now    -> renderVerdict / renderRates / renderStats / sparkline
 *   2. attention    -> renderAttention (warnings and failures, with the why)
 *   3. trend        -> chart (area + hover crosshair)
 *   4. who          -> renderProcesses, renderConnections (progressive detail)
 *   5. where        -> renderInterfaces
 *   6. monitor      -> renderCollectors / renderStorage / renderReadiness / host
 *
 * Rules this file follows:
 *   - Data is fetched in parallel with a timeout, and a failure of one endpoint
 *     never blanks the rest of the page: the panel keeps its last value and the
 *     banner says which sections are stale.
 *   - Nothing is written with innerHTML. All text goes through textContent, so
 *     a process name containing markup can never become markup.
 *   - Absolute URLs are never built; every request is same-origin and relative,
 *     which is what lets the dashboard work behind the preview proxy.
 */

"use strict";

/* ------------------------------------------------------------------ config */

const API = {
  status: "api/status",
  ready: "api/ready",
  traffic: "api/traffic",
  history: "api/traffic/history",
  interfaces: "api/interfaces",
  connections: "api/connections",
  events: "api/events",
  system: "api/system",
  event: (id) => `api/events/${id}`,
};

const REQUEST_TIMEOUT_MS = 6000;
const HISTORY_WINDOW_MS = 15 * 60 * 1000;
const MAX_CONNECTION_ROWS = 200;
const MAX_PROCESS_ROWS = 12;

const state = {
  intervalMs: 2000,
  timer: null,
  ticker: null,
  inFlight: false,
  lastSuccessAt: null,
  bootedAt: Date.now(),
  connections: [],
  connectionTotal: 0,
  events: [],
  history: [],
  failedSections: [],
  traffic: null,
  theme: "auto",
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------- utils */

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function replace(node, ...children) {
  node.replaceChildren(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
}

function bytes(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.abs(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  const decimals = unit === 0 ? 0 : size >= 100 ? 0 : 1;
  return `${value < 0 ? "-" : ""}${size.toFixed(decimals)} ${units[unit]}`;
}

function rate(value) {
  if (value === null || value === undefined) return "—";
  return `${bytes(value)}/s`;
}

function mbps(value) {
  if (value === null || value === undefined) return "—";
  return `${((value * 8) / 1e6).toFixed(2)} Mb/s`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${total % 60}s`;
  return `${total}s`;
}

function clock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString([], { hour12: false });
}

function relative(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.round(seconds)} s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

function count(value, noun) {
  return `${value} ${noun}${value === 1 ? "" : "s"}`;
}

async function getJson(path) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(path, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`${path} → HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

/* --------------------------------------------------------------- masthead */

function setDot(tone) {
  $("status-dot").dataset.state = tone;
}

function updateFreshness() {
  const node = $("freshness");
  const label = $("freshness-text");
  if (state.intervalMs === 0 && state.lastSuccessAt) {
    node.dataset.state = "waiting";
    label.textContent = `paused · updated ${relative(new Date(state.lastSuccessAt).toISOString())}`;
    return;
  }
  if (!state.lastSuccessAt) {
    node.dataset.state = state.failedSections.length ? "failed" : "waiting";
    label.textContent = state.failedSections.length
      ? "cannot reach the monitor"
      : "waiting for the first update";
    return;
  }
  const ageSeconds = (Date.now() - state.lastSuccessAt) / 1000;
  const intervalSeconds = Math.max(1, state.intervalMs / 1000);
  const age = relative(new Date(state.lastSuccessAt).toISOString());
  if (ageSeconds > intervalSeconds * 6) {
    node.dataset.state = "failed";
    label.textContent = `stale · last update ${age}`;
  } else if (ageSeconds > intervalSeconds * 3) {
    node.dataset.state = "stale";
    label.textContent = `lagging · last update ${age}`;
  } else {
    node.dataset.state = "fresh";
    label.textContent = `live · updated ${age}`;
  }
}

/* ------------------------------------------------ 1 · right now (the story) */

function renderVerdict(status, traffic, attention) {
  const headline = $("now-title");
  const detail = $("now-detail");
  const critical = attention.filter((event) => event.severity === "critical");
  const warnings = attention.filter((event) => event.severity === "warning");
  const [latestCritical] = critical;
  const [latestWarning] = warnings;

  if (latestCritical) {
    headline.dataset.tone = "crit";
    headline.textContent = "Something needs you now";
    detail.textContent =
      `${latestCritical.title} — ${relative(latestCritical.timestamp)}. ` +
      `${latestCritical.description} Open it below for the evidence.`;
    setDot("crit");
    return;
  }

  if (latestWarning) {
    headline.dataset.tone = "warn";
    headline.textContent = "Worth a look";
    detail.textContent =
      `${latestWarning.title} — ${relative(latestWarning.timestamp)}. ` +
      `Nothing is broken, but this is outside your configured thresholds.`;
    setDot("warn");
    return;
  }

  if (!status) {
    headline.dataset.tone = "warn";
    headline.textContent = "Waiting for the monitor";
    detail.textContent = "The dashboard has not reached the API yet. It retries on the refresh interval.";
    setDot("warn");
    return;
  }

  if (status.ready === false) {
    headline.dataset.tone = "warn";
    headline.textContent = "Collecting, but not fully ready";
    detail.textContent =
      "At least one readiness check is failing. Collection continues and the reason is listed under " +
      "\u201cThe monitor itself\u201d below.";
    setDot("warn");
    return;
  }

  const down = traffic?.download_bytes_per_second ?? 0;
  const up = traffic?.upload_bytes_per_second ?? 0;
  if (down <= 0 && up <= 0) {
    headline.dataset.tone = "ok";
    headline.textContent = "Quiet";
    detail.textContent =
      `No measurable traffic in the latest cycle (${clock(traffic?.timestamp ?? status.last_cycle_at)}). ` +
      `Nothing above your thresholds in the last 24 hours.`;
  } else {
    headline.dataset.tone = "work";
    headline.textContent = "Traffic is flowing";
    detail.textContent =
      `${rate(down)} down and ${rate(up)} up as of ${clock(traffic?.timestamp ?? status.last_cycle_at)}, ` +
      `with ${count(status.collectors ? Object.keys(status.collectors).length : 0, "collector")} reporting healthy.`;
  }
  setDot("ok");
}

function renderRates(traffic) {
  if (!traffic) return;
  state.traffic = traffic;
  $("metric-download").textContent = rate(traffic.download_bytes_per_second);
  $("metric-upload").textContent = rate(traffic.upload_bytes_per_second);
  $("metric-download-mbps").textContent = mbps(traffic.download_bytes_per_second);
  $("metric-upload-mbps").textContent = mbps(traffic.upload_bytes_per_second);
}

function renderStats(status, connectionsPayload, eventsPayload) {
  const collectorStates = Object.values(status?.collector_states || {});
  const failed = collectorStates.filter((value) => value === "failed").length;
  const degraded = collectorStates.filter((value) => value === "degraded").length;

  const processes = new Set(
    state.connections
      .filter((connection) => connection.pid !== null && connection.pid !== undefined)
      .map((connection) => connection.pid),
  ).size;

  $("metric-connections").textContent = connectionsPayload
    ? String(connectionsPayload.count ?? state.connections.length)
    : "—";
  $("metric-processes").textContent = connectionsPayload ? String(processes) : "—";

  const counts = eventsPayload?.counts_24h;
  $("metric-events").textContent = counts ? String(counts.total) : "—";
  $("metric-events-note").textContent = counts
    ? `${counts.critical} critical · ${counts.warning} warning · ${counts.info} info`
    : "\u00a0";

  $("metric-uptime").textContent = status ? duration(status.uptime_seconds) : "—";
  $("metric-cycles").textContent = status
    ? `${count(status.cycles ?? 0, "cycle")} · ${(status.collection_success_ratio ?? 1) * 100
    .toFixed(1)} % successful`
    : "\u00a0";

  const readiness = $("metric-readiness");
  if (!status) {
    readiness.textContent = "—";
    readiness.dataset.state = "unknown";
    $("metric-readiness-note").textContent = "\u00a0";
  } else {
    readiness.dataset.state = status.ready ? "ready" : "not-ready";
    readiness.textContent = status.ready ? "Ready" : "Not ready";
    const problems = [
      failed ? count(failed, "collector") + " failed" : "",
      degraded ? count(degraded, "collector") + " degraded" : "",
      status.last_errors?.length ? count(status.last_errors.length, "error") + " in the last cycle" : "",
    ].filter(Boolean);
    $("metric-readiness-note").textContent = problems.length ? problems.join(" · ") : "all checks passing";
  }

  $("subtitle").textContent =
    `${status?.storage?.database ?? "database"} · cycle ${status?.cycles ?? 0} · ` +
    `${status?.collection_interval_seconds ?? "?"} s interval`;
  $("footer-status").textContent = status
    ? `Local Network Monitor v${status.version} · last cycle ${clock(status.last_cycle_at)}` +
      (status.last_cycle_duration_ms ? ` (${status.last_cycle_duration_ms.toFixed(1)} ms)` : "")
    : "Local Network Monitor";
}

/* ------------------------------------------- 2 · attention (warnings first) */

const SEVERITY_GLYPH = { critical: "▲", warning: "●", info: "○" };

function humanLabel(key) {
  return key
    .replace(/_/g, " ")
    .replace(/\b(mbps|b|kb|gb|pid|tcp|id)\b/gi, (token) => token.toUpperCase())
    .replace(/^./, (char) => char.toUpperCase());
}

function eventMeta(event) {
  const bits = [event.source];
  if (event.interface_name) bits.push(event.interface_name);
  if (event.pid !== null && event.pid !== undefined) {
    bits.push(`PID ${event.pid}${event.process_name ? ` (${event.process_name})` : ""}`);
  }
  return bits.join(" · ");
}

function eventRow(event, { quiet = false } = {}) {
  const button = h(
    "button",
    {
      type: "button",
      class: `event event--${event.severity}${quiet ? " event--quiet" : ""}`,
      onclick: () => openEventDialog(event.id),
      "aria-label": `${event.severity} event: ${event.title}. Show evidence.`,
    },
    h("span", { class: "event-time", text: clock(event.timestamp) }),
    h("span", { class: `severity severity--${event.severity}` },
      h("span", { "aria-hidden": "true", text: SEVERITY_GLYPH[event.severity] || "•" }),
      h("span", { text: event.severity }),
    ),
    h("span", {},
      h("span", { class: "event-title", text: event.title }),
      // The "why" is the stored description, not something the UI invents.
      h("span", { class: "event-why", text: event.description }),
      h("span", { class: "event-meta", text: eventMeta(event) }),
    ),
  );
  return h("li", {}, button);
}

function renderAttention(events) {
  const attention = events.filter((event) => event.severity !== "info");
  const info = events.filter((event) => event.severity === "info");

  const list = $("attention-list");
  const empty = $("attention-empty");
  const note = $("attention-note");

  if (attention.length) {
    replace(list, attention.map((event) => eventRow(event)));
    list.hidden = false;
    empty.hidden = true;
    const critical = attention.filter((event) => event.severity === "critical").length;
    note.textContent = `${count(attention.length, "event")} in the last 24 h · ${critical} critical`;
  } else {
    replace(list);
    list.hidden = true;
    empty.hidden = false;
    note.textContent = "none in the last 24 h";
  }

  const quietList = $("events-body");
  replace(
    quietList,
    info.length
      ? info.map((event) => eventRow(event, { quiet: true }))
      : [h("li", { class: "empty", text: "no informational events in this window" })],
  );
  $("info-event-count").textContent = String(info.length);

  return attention;
}

/* ---------------------------------------------------------- 3 · the trend */

const chart = {
  canvas: null,
  ctx: null,
  series: [],
  hover: null,
  padding: { top: 14, right: 16, bottom: 26, left: 72 },

  init() {
    this.canvas = $("traffic-chart");
    this.ctx = this.canvas.getContext("2d");
    window.addEventListener("resize", () => this.draw());
    this.canvas.addEventListener("pointermove", (event) => this.onMove(event));
    this.canvas.addEventListener("pointerleave", () => { this.hover = null; this.draw(); });
  },

  setPoints(points) {
    const buckets = new Map();
    for (const point of points) {
      const bucket = buckets.get(point.timestamp) || { download: 0, upload: 0 };
      bucket.download += point.download_bytes_per_second || 0;
      bucket.upload += point.upload_bytes_per_second || 0;
      buckets.set(point.timestamp, bucket);
    }
    this.series = [...buckets.entries()]
      .sort((a, b) => new Date(a[0]) - new Date(b[0]))
      .map(([timestamp, value]) => ({ time: new Date(timestamp), ...value }));
    this.draw();
    this.describe();
  },

  describe() {
    const note = $("chart-note");
    const sparkCaption = $("spark-caption");
    if (!this.series.length) {
      note.textContent = "collecting the first samples…";
      sparkCaption.textContent = "waiting for samples";
      return;
    }
    sparkCaption.textContent = `last 15 minutes · ${this.series.length} samples`;
    const peak = Math.max(1024, ...this.series.map((point) => Math.max(point.download, point.upload)));
    const first = this.series[0].time;
    const last = this.series.at(-1).time;
    note.textContent =
      `${this.series.length} samples · ${clock(first.toISOString())} → ${clock(last.toISOString())} · ` +
      `peak ${rate(peak)} · all interfaces combined`;
  },

  onMove(event) {
    if (!this.series.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const plotWidth = rect.width - this.padding.left - this.padding.right;
    const ratio = this.series.length === 1
      ? 0
      : Math.min(1, Math.max(0, (x - this.padding.left) / plotWidth));
    this.hover = Math.round(ratio * (this.series.length - 1));
    this.draw();
    this.tooltip(event.clientX - rect.left, rect.width);
  },

  tooltip(x, width) {
    const tooltip = $("chart-tooltip");
    const point = this.series[this.hover];
    if (!point) { tooltip.hidden = true; return; }
    replace(
      tooltip,
      h("strong", { text: clock(point.time.toISOString()) }),
      h("div", { text: `↓ ${rate(point.download)}` }),
      h("div", { text: `↑ ${rate(point.upload)}` }),
    );
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(Math.max(x, 70), width - 70)}px`;
    tooltip.style.top = `${this.padding.top + 8}px`;
  },

  draw() {
    const canvas = this.canvas;
    if (!canvas || !this.ctx) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 640;
    const height = canvas.clientHeight || 200;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = this.ctx;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const styles = getComputedStyle(document.documentElement);
    const colour = {
      down: styles.getPropertyValue("--down").trim() || "#38bdf8",
      up: styles.getPropertyValue("--up").trim() || "#a78bfa",
      muted: styles.getPropertyValue("--text-dim").trim() || "#6b7b96",
      grid: styles.getPropertyValue("--line-soft").trim() || "#1c2536",
      text: styles.getPropertyValue("--text").trim() || "#e8eefb",
    };

    if (!this.series.length) {
      ctx.fillStyle = colour.muted;
      ctx.font = "13px 'Segoe UI', system-ui, sans-serif";
      ctx.fillText("collecting the first samples…", this.padding.left, height / 2);
      return;
    }

    const { top, right, bottom, left } = this.padding;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const maxValue = Math.max(1024, ...this.series.map((point) => Math.max(point.download, point.upload)));

    // Horizontal gridlines with byte-rate labels.
    ctx.strokeStyle = colour.grid;
    ctx.fillStyle = colour.muted;
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = Math.round(top + (plotHeight / 4) * i) + 0.5;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + plotWidth, y);
      ctx.stroke();
      ctx.fillText(`${bytes(maxValue * (1 - i / 4))}/s`, 8, y + 4);
    }

    const xFor = (index) =>
      left + (this.series.length === 1 ? plotWidth / 2 : (plotWidth * index) / (this.series.length - 1));
    const yFor = (value) => top + plotHeight - (plotHeight * value) / maxValue;

    // x-axis: first, middle and last timestamp so the window is unambiguous.
    ctx.fillStyle = colour.muted;
    [0, Math.floor(this.series.length / 2), this.series.length - 1].forEach((index) => {
      const point = this.series[index];
      if (!point) return;
      const label = clock(point.time.toISOString());
      const x = xFor(index);
      ctx.fillText(label, Math.min(Math.max(x - 20, left), left + plotWidth - 40), height - 8);
    });

    const area = (key, fill, stroke) => {
      ctx.beginPath();
      this.series.forEach((point, index) => {
        const x = xFor(index);
        const y = yFor(point[key]);
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      const gradient = ctx.createLinearGradient(0, top, 0, top + plotHeight);
      gradient.addColorStop(0, fill);
      gradient.addColorStop(1, "rgba(0,0,0,0)");
      ctx.save();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.lineTo(xFor(this.series.length - 1), top + plotHeight);
      ctx.lineTo(xFor(0), top + plotHeight);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();
      ctx.restore();
    };

    area("download", hexToRgba(colour.down, 0.28), colour.down);
    area("upload", hexToRgba(colour.up, 0.24), colour.up);

    // Hover crosshair: the detail layer of the chart.
    if (this.hover !== null && this.series[this.hover]) {
      const point = this.series[this.hover];
      const x = xFor(this.hover);
      ctx.save();
      ctx.strokeStyle = colour.muted;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, top + plotHeight);
      ctx.stroke();
      ctx.setLineDash([]);
      [["download", colour.down], ["upload", colour.up]].forEach(([key, stroke]) => {
        ctx.beginPath();
        ctx.fillStyle = stroke;
        ctx.arc(x, yFor(point[key]), 3.5, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    }
  },
};

const spark = {
  canvas: null,
  ctx: null,
  series: [],
  init() {
    this.canvas = $("sparkline");
    this.ctx = this.canvas.getContext("2d");
    window.addEventListener("resize", () => this.draw());
  },
  setSeries(series) { this.series = series; this.draw(); },
  draw() {
    const canvas = this.canvas;
    if (!canvas || !this.ctx) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 320;
    const height = canvas.clientHeight || 72;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    const ctx = this.ctx;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const styles = getComputedStyle(document.documentElement);
    const colour = {
      down: styles.getPropertyValue("--down").trim() || "#38bdf8",
      up: styles.getPropertyValue("--up").trim() || "#a78bfa",
      grid: styles.getPropertyValue("--line-soft").trim() || "#1c2536",
    };

    ctx.strokeStyle = colour.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, height - 1.5);
    ctx.lineTo(width, height - 1.5);
    ctx.stroke();

    if (this.series.length < 2) return;
    const maxValue = Math.max(1024, ...this.series.map((point) => Math.max(point.download, point.upload)));
    const xFor = (index) => (width * index) / (this.series.length - 1);
    const yFor = (value) => height - 4 - ((height - 10) * value) / maxValue;

    [["download", colour.down], ["upload", colour.up]].forEach(([key, stroke]) => {
      ctx.beginPath();
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.6;
      ctx.lineJoin = "round";
      this.series.forEach((point, index) => {
        const x = xFor(index);
        const y = yFor(point[key]);
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
  },
};

function hexToRgba(value, alpha) {
  const hex = String(value).trim().replace("#", "");
  if (hex.length === 6) {
    const int = parseInt(hex, 16);
    return `rgba(${(int >> 16) & 255}, ${(int >> 8) & 255}, ${int & 255}, ${alpha})`;
  }
  if (hex.startsWith("rgb")) return value.replace(/rgba?\(([^)]+)\)/, `rgba($1, ${alpha})`);
  return `rgba(56, 189, 248, ${alpha})`;
}

/* ---------------------------------------------------------------- 4 · who */

function aggregateProcesses(connections) {
  const byProcess = new Map();
  for (const connection of connections) {
    const name = connection.process_name || "unknown";
    const pid = connection.pid === null || connection.pid === undefined ? null : connection.pid;
    const key = `${pid ?? "none"}::${name}`;
    const entry = byProcess.get(key) || {
      name, pid, connections: 0, states: new Set(), hosts: new Set(),
    };
    entry.connections += 1;
    if (connection.state) entry.states.add(connection.state);
    if (connection.remote_address) {
      entry.hosts.add(`${connection.remote_address}:${connection.remote_port}`);
    }
    byProcess.set(key, entry);
  }
  return [...byProcess.values()].sort(
    (a, b) => b.connections - a.connections || a.name.localeCompare(b.name),
  );
}

function renderProcesses(connections) {
  const list = $("process-list");
  const empty = $("process-empty");
  const processes = aggregateProcesses(connections);

  if (!processes.length) {
    replace(list);
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  const rows = processes.slice(0, MAX_PROCESS_ROWS).map((entry, index) => {
    const hosts = [...entry.hosts].slice(0, 3).join("  ");
    const moreHosts = entry.hosts.size > 3 ? ` +${entry.hosts.size - 3} more` : "";
    return h(
      "li",
      { class: "process" },
      h("span", { class: "process-rank", text: String(index + 1) }),
      h("span", { class: "process-name", title: entry.name },
        entry.name,
        " ",
        h("span", { class: "pid", text: entry.pid === null ? "PID unknown" : `PID ${entry.pid}` }),
      ),
      h("span", { class: "process-metric" }, String(entry.connections), h("span", { text: "connections" })),
      h("span", { class: "process-metric" }, String(entry.hosts.size), h("span", { text: "remotes" })),
      h("span", { class: "process-metric" }, String(entry.states.size), h("span", { text: "states" })),
      h("span", { class: "process-hosts", title: [...entry.hosts].join("\n"), text: hosts + moreHosts }),
    );
  });

  if (processes.length > MAX_PROCESS_ROWS) {
    rows.push(
      h("li", { class: "process process--more" },
        h("span", { class: "process-hosts", text: `+ ${processes.length - MAX_PROCESS_ROWS} more processes in the table below` })),
    );
  }
  replace(list, rows);
}

function renderConnections() {
  const body = $("connections-body");
  const processFilter = $("filter-process").value.trim().toLowerCase();
  const remoteFilter = $("filter-remote").value.trim().toLowerCase();
  const stateFilter = $("filter-state").value;

  const rows = state.connections.filter((connection) => {
    const name = (connection.process_name || `pid ${connection.pid ?? "unknown"}`).toLowerCase();
    if (processFilter && !name.includes(processFilter)) return false;
    if (remoteFilter) {
      const remote = `${connection.remote_address ?? ""}:${connection.remote_port ?? ""}`.toLowerCase();
      if (!remote.includes(remoteFilter)) return false;
    }
    if (stateFilter && connection.state !== stateFilter) return false;
    return true;
  });

  $("connection-count").textContent = String(state.connectionTotal);
  $("filter-count").textContent = rows.length === state.connections.length
    ? `${state.connections.length} shown`
    : `${rows.length} of ${state.connections.length} shown`;

  if (!rows.length) {
    replace(body, h("tr", {}, h("td", { colspan: "5", class: "empty", text: "no matching connections" })));
    return;
  }

  const visible = rows.slice(0, MAX_CONNECTION_ROWS).map((connection) => {
    const remote = connection.remote_address
      ? `${connection.remote_address}:${connection.remote_port}`
      : "—";
    const known = connection.pid !== null && connection.pid !== undefined;
    return h(
      "tr",
      {},
      h("td", { class: known ? "" : "muted", text: connection.process_name || "unknown" }),
      h("td", { class: "num", text: known ? String(connection.pid) : "—" }),
      h("td", { class: "mono muted", text: connection.local_endpoint || "—" }),
      h("td", { class: "mono", text: remote }),
      h("td", { text: connection.state }),
    );
  });

  if (rows.length > MAX_CONNECTION_ROWS) {
    visible.push(
      h("tr", {}, h("td", {
        colspan: "5",
        class: "empty",
        text: `showing the first ${MAX_CONNECTION_ROWS} of ${rows.length} matching connections`,
      })),
    );
  }
  replace(body, visible);
}

/* -------------------------------------------------------------- 5 · where */

function renderInterfaces(interfaces) {
  const body = $("interfaces-body");
  const note = $("interfaces-note");

  if (!interfaces.length) {
    replace(body, h("tr", {}, h("td", { colspan: "7", class: "empty", text: "no interfaces detected" })));
    note.textContent = "none detected";
    return;
  }

  const up = interfaces.filter((nic) => nic.is_up).length;
  const looping = interfaces.filter((nic) => nic.is_loopback).length;
  note.textContent = `${up} up · ${interfaces.length - up} down · ${looping} loopback`;

  replace(body, interfaces.map((nic) => {
    const errors = [nic.errors_in, nic.errors_out, nic.drops_in, nic.drops_out]
      .map((value) => value ?? 0)
      .reduce((total, value) => total + value, 0);
    const state = h("span", { class: "state-chip", "data-state": nic.is_up ? "healthy" : "failed" },
      h("span", { "aria-hidden": "true", text: nic.is_up ? "▲" : "▼" }),
      nic.is_up === null ? "unknown" : nic.is_up ? "up" : "down");
    const addresses = (nic.addresses || []).join(", ");
    return h(
      "tr",
      {},
      h("td", { class: "mono" }, nic.name, nic.is_loopback ? h("span", { class: "muted", text: "  loopback" }) : ""),
      h("td", {}, state),
      h("td", { class: "num num--down", text: rate(nic.download_bytes_per_second) }),
      h("td", { class: "num num--up", text: rate(nic.upload_bytes_per_second) }),
      h("td", { class: "num", text: bytes(nic.bytes_received) }),
      h("td", { class: "num", text: bytes(nic.bytes_sent) }),
      h("td", { class: `num${errors ? " num--warn" : ""}`, text: String(errors) }),
      h("td", { class: "addresses", title: addresses, text: addresses }),
    );
  }));
}

/* ------------------------------------------------------- 6 · monitor health */

function renderCollectors(status) {
  const body = $("collectors-body");
  const collectors = Object.values(status?.collectors || {});
  if (!collectors.length) {
    replace(body, h("tr", {}, h("td", { colspan: "6", class: "empty", text: "no collector data yet" })));
    $("health-note").textContent = "no data yet";
    return;
  }

  const failing = collectors.filter((collector) => collector.state !== "healthy");
  $("health-note").textContent = failing.length
    ? `${count(failing.length, "collector")} not healthy`
    : `all ${collectors.length} collectors healthy`;

  replace(body, collectors.map((collector) => h(
    "tr",
    {},
    h("td", {}, h("span", { class: "mono", text: collector.name })),
    h("td", {}, h("span", { class: "state-chip", "data-state": collector.state }, collector.state)),
    h("td", { class: "num", text: String(collector.total_runs ?? 0) }),
    h("td", { class: `num${collector.total_failures ? " num--warn" : ""}`, text: String(collector.total_failures ?? 0) }),
    h("td", { class: "num", text: relative(collector.last_success) }),
    h("td", { class: "num", text: collector.last_duration_ms ? `${collector.last_duration_ms.toFixed(1)} ms` : "—" }),
  )));
}

function renderKeyValues(node, pairs) {
  replace(node, pairs.flatMap(([key, value]) => [
    h("dt", { text: key }),
    h("dd", { text: value === null || value === undefined ? "—" : String(value) }),
  ]));
}

function renderMonitorDetails(status, readyPayload, system) {
  const storage = status?.storage || {};
  renderKeyValues($("storage-list"), [
    ["Database", storage.database || "—"],
    ["Measurements", storage.interface_measurements ?? "—"],
    ["Connections", storage.connections ?? "—"],
    ["Processes", storage.processes ?? "—"],
    ["Events", storage.events ?? "—"],
    ["Retention", "7 d measurements/events · 24 h connections"],
  ]);

  const checks = readyPayload?.checks || {};
  renderKeyValues($("readiness-list"), Object.keys(checks).length
    ? Object.entries(checks).map(([name, ok]) => [humanLabel(name), ok ? "pass" : "fail"])
    : [["Probe", "unavailable"]]);

  renderKeyValues($("host-list"), system
    ? [
      ["Host", system.hostname],
      ["Platform", system.platform],
      ["Python", system.python_version],
      ["CPU", `${system.cpu_percent} %`],
      ["Memory", `${system.memory_percent} %`],
      ["Processes", system.process_count],
      ["Uptime", duration(system.uptime_seconds)],
    ]
    : [["Host", "unavailable"]]);
}

/* ---------------------------------------------------------- event dialog */

const EVIDENCE_LABELS = {
  interface: "Interface",
  download_rate_mbps: "Download rate (Mb/s)",
  upload_rate_mbps: "Upload rate (Mb/s)",
  threshold_mbps: "Configured threshold (Mb/s)",
  download_rate_bytes_per_second: "Download rate (B/s)",
  upload_rate_bytes_per_second: "Upload rate (B/s)",
  threshold_bytes_per_second: "Configured threshold (B/s)",
  baseline_connections: "Baseline connections",
  current_connections: "Connections now",
  multiplier: "Multiplier",
  observed_ratio: "Observed ratio",
  pid: "PID",
  process_name: "Process",
  consecutive_failures: "Consecutive failures",
  last_error: "Last error",
  collector: "Collector",
  state: "State",
  timestamp: "Timestamp",
};

function renderEvidence(evidence) {
  const node = $("dialog-evidence");
  if (!evidence || !Object.keys(evidence).length) {
    replace(node, h("dt", { text: "No structured evidence recorded for this event." }));
    return;
  }
  const pairs = Object.entries(evidence)
    .filter(([, value]) => value !== null && value !== undefined && typeof value !== "object")
    .map(([key, value]) => [EVIDENCE_LABELS[key] || humanLabel(key), value]);
  const nested = Object.entries(evidence)
    .filter(([, value]) => value !== null && typeof value === "object")
    .map(([key, value]) => [EVIDENCE_LABELS[key] || humanLabel(key), JSON.stringify(value)]);
  replace(node, [...pairs, ...nested].flatMap(([key, value]) => [
    h("dt", { text: key }),
    h("dd", { text: String(value) }),
  ]));
  $("dialog-raw").textContent = JSON.stringify(evidence, null, 2);
}

async function openEventDialog(eventId) {
  try {
    const event = await getJson(API.event(eventId));
    $("dialog-severity").textContent = `${event.event_type} · ${event.severity} · ${event.status}`;
    $("dialog-title").textContent = event.title;
    $("dialog-meta").textContent =
      `#${event.id} · ${new Date(event.timestamp).toLocaleString()} · ${eventMeta(event)}`;
    $("dialog-description").textContent = event.description;
    renderEvidence(event.evidence);
    const dialog = $("event-dialog");
    // <dialog> is supported everywhere this ships, but a dashboard that throws
    // on an older browser instead of showing the evidence is a worse trade than
    // one extra guard.
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  } catch (error) {
    showBanner(`Could not load event #${eventId}: ${error.message}`);
  }
}

/* ------------------------------------------------------------- refresh loop */

function showBanner(message) {
  const banner = $("error-banner");
  replace(banner, h("strong", { text: "Partial data. " }), message);
  banner.hidden = false;
}

function clearBanner() {
  const banner = $("error-banner");
  banner.hidden = true;
  replace(banner);
}

async function refresh() {
  if (state.inFlight) return;
  state.inFlight = true;
  $("main").setAttribute("aria-busy", "true");

  const since = new Date(Date.now() - HISTORY_WINDOW_MS).toISOString();
  const requests = {
    status: API.status,
    ready: API.ready,
    traffic: API.traffic,
    interfaces: API.interfaces,
    connections: `${API.connections}?limit=2000`,
    history: `${API.history}?from=${encodeURIComponent(since)}&limit=900`,
    events: `${API.events}?limit=100`,
    system: API.system,
  };

  const results = await Promise.allSettled(
    Object.entries(requests).map(async ([key, path]) => [key, await getJson(path)]),
  );

  const data = {};
  const failed = [];
  results.forEach((result, index) => {
    const key = Object.keys(requests)[index];
    if (result.status === "fulfilled") data[key] = result.value[1];
    else failed.push(key);
  });

  state.failedSections = failed;
  const status = data.status;
  const connectionsPayload = data.connections;
  const eventsPayload = data.events;

  if (connectionsPayload) {
    state.connections = connectionsPayload.connections || [];
    state.connectionTotal = connectionsPayload.count ?? state.connections.length;
  }

  // Sections that did get data are rendered immediately; the rest keep their
  // previous content rather than flashing empty.
  if (data.traffic) renderRates(data.traffic);
  if (data.interfaces) renderInterfaces(data.interfaces);
  if (connectionsPayload) {
    renderProcesses(state.connections);
    renderConnections();
  }
  if (data.history) {
    chart.setPoints(data.history.points || []);
    spark.setSeries(chart.series);
  }
  if (status) renderCollectors(status);
  if (status || data.ready || data.system) renderMonitorDetails(status, data.ready, data.system);

  let attention = [];
  if (eventsPayload) {
    attention = renderAttention(eventsPayload.events || []);
    state.events = eventsPayload.events || [];
  }
  if (status || data.traffic || eventsPayload) {
    renderVerdict(status, data.traffic || state.traffic, attention.length ? attention : state.events.filter((e) => e.severity !== "info"));
  }
  if (status || connectionsPayload || eventsPayload) {
    renderStats(status, connectionsPayload, eventsPayload);
  }

  if (failed.length) {
    showBanner(
      `These sections could not be refreshed: ${failed.join(", ")}. ` +
      `They keep their last known values; retrying in ${state.intervalMs ? `${state.intervalMs / 1000} s` : "manual refresh"}.`,
    );
  } else {
    clearBanner();
    state.lastSuccessAt = Date.now();
  }

  updateFreshness();
  document.body.dataset.state = "ready";
  $("main").setAttribute("aria-busy", "false");
  state.inFlight = false;
}

function schedule() {
  if (state.timer) clearInterval(state.timer);
  state.timer = state.intervalMs > 0 ? setInterval(refresh, state.intervalMs) : null;
}

/* ------------------------------------------------------------------ theme */

const THEMES = ["auto", "light", "dark"];

function applyTheme(theme) {
  state.theme = THEMES.includes(theme) ? theme : "auto";
  document.documentElement.dataset.theme = state.theme;
  $("theme-toggle").textContent = `Theme: ${state.theme}`;
  try { localStorage.setItem("lnm-theme", state.theme); } catch (_) { /* private mode */ }
  chart.draw();
  spark.draw();
}

/* ----------------------------------------------------------------- start-up */

function init() {
  spark.init();
  chart.init();

  try { applyTheme(localStorage.getItem("lnm-theme") || "auto"); } catch (_) { applyTheme("auto"); }

  $("refresh-interval").addEventListener("change", (event) => {
    state.intervalMs = Number(event.target.value);
    schedule();
    updateFreshness();
    if (state.intervalMs > 0) refresh();
  });
  $("refresh-now").addEventListener("click", refresh);
  $("theme-toggle").addEventListener("click", () => {
    applyTheme(THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length]);
  });

  ["filter-process", "filter-remote", "filter-state"].forEach((id) =>
    $(id).addEventListener("input", renderConnections),
  );

  $("dialog-close").addEventListener("click", () => {
    const dialog = $("event-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  });
  $("event-dialog").addEventListener("click", (event) => {
    if (event.target === $("event-dialog")) $("event-dialog").close();
  });

  updateFreshness();
  state.ticker = setInterval(updateFreshness, 1000);
  refresh();
  schedule();
}

document.addEventListener("DOMContentLoaded", init);
