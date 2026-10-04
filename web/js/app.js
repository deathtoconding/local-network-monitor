/* Local Network Monitor - dashboard logic.
 *
 * Plain ES2020, no frameworks and no CDN: the whole UI is three fetches against
 * the same-origin API (/api/status, /api/traffic/history, /api/connections) plus
 * the events feed. Relative URLs only - the dashboard must work behind the
 * sandbox/preview proxy as well as on http://127.0.0.1:8000.
 */

"use strict";

const API = {
  status: "api/status",
  traffic: "api/traffic",
  history: "api/traffic/history",
  interfaces: "api/interfaces",
  connections: "api/connections",
  events: "api/events",
  event: (id) => `api/events/${id}`,
};

const state = {
  timer: null,
  intervalMs: 2000,
  inFlight: false,
  connections: [],
  lastStatus: null,
};

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ utils */
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Math.abs(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const sign = bytes < 0 ? "-" : "";
  return `${sign}${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatRate(bytesPerSecond) {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return "—";
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatMegabits(bytesPerSecond) {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return "—";
  return `${((bytesPerSecond * 8) / 1e6).toFixed(2)} Mbps`;
}

function formatTotal(bytes) {
  return formatBytes(bytes);
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function timeOf(isoString) {
  if (!isoString) return "—";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString([], { hour12: false });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function getJson(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path} -> HTTP ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------- rendering */
function renderStatus(status, connectionsCount) {
  state.lastStatus = status;
  const collectorStates = Object.values(status.collector_states || {});
  const failed = collectorStates.filter((s) => s === "failed").length;
  const degraded = collectorStates.filter((s) => s === "degraded").length;

  const dot = $("status-dot");
  dot.className = "dot " + (failed ? "bad" : degraded ? "warn" : "ok");

  $("metric-status").textContent = failed
    ? `degraded (${failed} collector${failed > 1 ? "s" : ""} failed)`
    : degraded
      ? `degraded (${degraded})`
      : "healthy";
  $("metric-connections").textContent = connectionsCount ?? "—";
  $("metric-events").textContent = status.events_24h ? status.events_24h.total : "—";
  $("metric-uptime").textContent = formatDuration(status.uptime_seconds);

  $("subtitle").textContent =
    `${status.storage?.database ?? "database"} · cycle ` +
    `${status.cycles ?? 0} · ${status.collection_interval_seconds ?? "?"}s interval`;
  $("footer-status").textContent =
    `Local Network Monitor v${status.version} · last cycle ` +
    `${status.last_cycle_at ? timeOf(status.last_cycle_at) : "never"}` +
    (status.last_cycle_duration_ms ? ` (${status.last_cycle_duration_ms.toFixed(1)} ms)` : "");

  const list = $("collector-list");
  list.innerHTML = Object.entries(status.collectors || {})
    .map(([name, info]) => {
      const since = info.last_success ? timeOf(info.last_success) : "never";
      const title = info.last_error ? ` title="${escapeHtml(info.last_error)}"` : "";
      return `<li${title}>${escapeHtml(name)} <span class="state ${escapeHtml(info.state)}">` +
        `${escapeHtml(info.state)}</span> <span class="muted">${since}</span></li>`;
    })
    .join("");
}

function renderTraffic(traffic) {
  $("metric-download").textContent = formatRate(traffic.download_bytes_per_second);
  $("metric-upload").textContent = formatRate(traffic.upload_bytes_per_second);
  $("metric-download").title = formatMegabits(traffic.download_bytes_per_second);
  $("metric-upload").title = formatMegabits(traffic.upload_bytes_per_second);
}

function renderInterfaces(interfaces) {
  const body = $("interfaces-body");
  if (!interfaces.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">no interfaces detected</td></tr>';
    return;
  }
  body.innerHTML = interfaces
    .map((nic) => {
      const up = nic.is_up === null ? "?" : nic.is_up ? "up" : "down";
      const errors = [nic.errors_in, nic.errors_out, nic.drops_in, nic.drops_out]
        .map((value) => value ?? 0)
        .reduce((a, b) => a + b, 0);
      return `<tr>
        <td class="mono">${escapeHtml(nic.name)}</td>
        <td class="${up === "up" ? "muted" : "muted"}">${up}</td>
        <td class="num accent-down">${formatRate(nic.download_bytes_per_second)}</td>
        <td class="num accent-up">${formatRate(nic.upload_bytes_per_second)}</td>
        <td class="num">${formatTotal(nic.bytes_received)}</td>
        <td class="num">${formatTotal(nic.bytes_sent)}</td>
        <td class="num">${errors}</td>
      </tr>`;
    })
    .join("");
}

function renderConnections() {
  const body = $("connections-body");
  const processFilter = $("filter-process").value.trim().toLowerCase();
  const remoteFilter = $("filter-remote").value.trim().toLowerCase();
  const stateFilter = $("filter-state").value;

  const rows = state.connections.filter((connection) => {
    const name = (connection.process_name || `pid ${connection.pid ?? "?"}`).toLowerCase();
    if (processFilter && !name.includes(processFilter)) return false;
    if (remoteFilter) {
      const remote = `${connection.remote_address}:${connection.remote_port}`.toLowerCase();
      if (!remote.includes(remoteFilter)) return false;
    }
    if (stateFilter && connection.state !== stateFilter) return false;
    return true;
  });

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">no matching connections</td></tr>';
    return;
  }

  body.innerHTML = rows
    .slice(0, 300)
    .map((connection) => {
      const remote = connection.remote_address
        ? `${connection.remote_address}:${connection.remote_port}`
        : "—";
      const processName = connection.process_name || "unknown";
      const unknownClass = /^</.test(processName) || processName === "unknown" ? "muted" : "";
      return `<tr>
        <td class="${unknownClass}">${escapeHtml(processName)}</td>
        <td class="num">${connection.pid ?? "—"}</td>
        <td class="mono muted">${escapeHtml(connection.local_endpoint || "—")}</td>
        <td class="mono">${escapeHtml(remote)}</td>
        <td>${escapeHtml(connection.state)}</td>
      </tr>`;
    })
    .join("");

  if (rows.length > 300) {
    body.insertAdjacentHTML(
      "beforeend",
      `<tr><td colspan="5" class="empty">showing first 300 of ${rows.length} connections</td></tr>`,
    );
  }
}

function renderEvents(payload) {
  const list = $("events-body");
  const severityFilter = $("filter-severity").value;
  const events = (payload.events || []).filter(
    (event) => !severityFilter || event.severity === severityFilter,
  );

  if (!events.length) {
    list.innerHTML = '<li class="empty">no events match the current filter</li>';
    return;
  }

  list.innerHTML = events
    .map(
      (event) => `<li class="severity-${escapeHtml(event.severity)}" data-event-id="${event.id}">
        <span class="event-time">${timeOf(event.timestamp)}</span>
        <span class="event-sev">${escapeHtml(event.severity)}</span>
        <span>${escapeHtml(event.title)}
          <span class="event-desc">${escapeHtml(event.description)}</span>
        </span>
      </li>`,
    )
    .join("");
}

/* ---------------------------------------------------------------- chart */
const chart = {
  canvas: null,
  ctx: null,
  points: [],

  init() {
    this.canvas = $("traffic-chart");
    this.ctx = this.canvas.getContext("2d");
    window.addEventListener("resize", () => this.draw());
  },

  setPoints(points) {
    this.points = points;
    this.draw();
  },

  draw() {
    const canvas = this.canvas;
    if (!canvas || !this.ctx) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 180;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = this.ctx;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const points = this.points;
    if (!points.length) {
      ctx.fillStyle = "#8a99b5";
      ctx.font = "13px Segoe UI, system-ui, sans-serif";
      ctx.fillText("collecting data…", 12, height / 2);
      return;
    }

    // Aggregate all interfaces per timestamp so the chart reflects the host.
    const buckets = new Map();
    for (const point of points) {
      const key = point.timestamp;
      const bucket = buckets.get(key) || { download: 0, upload: 0 };
      bucket.download += point.download_bytes_per_second || 0;
      bucket.upload += point.upload_bytes_per_second || 0;
      buckets.set(key, bucket);
    }
    const series = [...buckets.entries()]
      .sort((a, b) => new Date(a[0]) - new Date(b[0]))
      .map(([timestamp, value]) => ({ timestamp: new Date(timestamp), ...value }));

    const maxValue = Math.max(
      1024,
      ...series.map((point) => Math.max(point.download, point.upload)),
    );
    const padding = { top: 12, right: 12, bottom: 22, left: 64 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;

    // grid + axis labels
    ctx.strokeStyle = "#1f2a3d";
    ctx.fillStyle = "#8a99b5";
    ctx.font = "11px ui-monospace, Consolas, monospace";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = padding.top + (plotHeight / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(padding.left + plotWidth, y);
      ctx.stroke();
      const value = maxValue * (1 - i / 4);
      ctx.fillText(formatBytes(value) + "/s", 6, y + 4);
    }

    const xFor = (index) =>
      padding.left + (series.length === 1 ? plotWidth / 2 : (plotWidth * index) / (series.length - 1));
    const yFor = (value) => padding.top + plotHeight - (plotHeight * value) / maxValue;

    const line = (key, colour) => {
      ctx.beginPath();
      ctx.strokeStyle = colour;
      ctx.lineWidth = 2;
      series.forEach((point, index) => {
        const x = xFor(index);
        const y = yFor(point[key]);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    };

    line("download", "#38bdf8");
    line("upload", "#a78bfa");

    $("chart-note").textContent =
      `last ${series.length} samples · peak ${formatBytes(maxValue)}/s · ` +
      `${timeOf(series[0].timestamp.toISOString())} → ${timeOf(series.at(-1).timestamp.toISOString())}`;
  },
};

/* ------------------------------------------------------------ event dialog */
async function openEventDialog(eventId) {
  try {
    const event = await getJson(API.event(eventId));
    $("dialog-title").textContent = event.title;
    $("dialog-meta").textContent =
      `#${event.id} · ${event.event_type} · ${event.severity.toUpperCase()} · ` +
      `${new Date(event.timestamp).toLocaleString()} · source ${event.source}` +
      (event.pid !== null ? ` · PID ${event.pid} (${event.process_name || "unknown"})` : "") +
      (event.interface_name ? ` · ${event.interface_name}` : "");
    $("dialog-description").textContent = event.description;
    $("dialog-evidence").textContent = JSON.stringify(event.evidence, null, 2);
    $("event-dialog").showModal();
  } catch (error) {
    console.error(error);
  }
}

/* ------------------------------------------------------------------ refresh */
async function refresh() {
  if (state.inFlight) return;
  state.inFlight = true;
  const errors = [];

  try {
    const status = await getJson(API.status);
    renderStatus(status, null);
  } catch (error) {
    errors.push(error);
    $("status-dot").className = "dot bad";
    $("metric-status").textContent = "monitor unreachable";
  }

  try {
    const traffic = await getJson(API.traffic);
    renderTraffic(traffic);
  } catch (error) {
    errors.push(error);
  }

  try {
    const interfaces = await getJson(API.interfaces);
    renderInterfaces(interfaces);
  } catch (error) {
    errors.push(error);
  }

  try {
    const connections = await getJson(API.connections);
    state.connections = connections.connections || [];
    renderConnections();
    $("metric-connections").textContent = connections.count ?? state.connections.length;
  } catch (error) {
    errors.push(error);
  }

  try {
    const since = new Date(Date.now() - 15 * 60 * 1000).toISOString();
    const history = await getJson(`${API.history}?from=${encodeURIComponent(since)}&limit=900`);
    chart.setPoints(history.points || []);
  } catch (error) {
    errors.push(error);
  }

  try {
    const events = await getJson(`${API.events}?limit=50`);
    renderEvents(events);
  } catch (error) {
    errors.push(error);
  }

  if (errors.length) console.warn("partial refresh failure:", errors);
  state.inFlight = false;
}

function scheduleRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (state.intervalMs > 0) {
    state.timer = setInterval(refresh, state.intervalMs);
  }
}

/* ----------------------------------------------------------------- start-up */
function init() {
  chart.init();

  $("refresh-interval").addEventListener("change", (event) => {
    state.intervalMs = Number(event.target.value);
    scheduleRefresh();
  });
  $("refresh-now").addEventListener("click", refresh);
  ["filter-process", "filter-remote", "filter-state"].forEach((id) =>
    $(id).addEventListener("input", renderConnections),
  );
  $("filter-severity").addEventListener("change", () => refresh());
  $("events-body").addEventListener("click", (event) => {
    const row = event.target.closest("li[data-event-id]");
    if (row) openEventDialog(row.dataset.eventId);
  });
  $("dialog-close").addEventListener("click", () => $("event-dialog").close());

  refresh();
  scheduleRefresh();
}

document.addEventListener("DOMContentLoaded", init);
