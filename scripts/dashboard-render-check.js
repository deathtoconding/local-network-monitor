#!/usr/bin/env node
/*
 * Render-check for the dashboard.
 *
 * The dashboard is plain HTML/CSS/JS, so nothing at build time would notice a
 * script that queries a missing element, a table that lost its caption, or a
 * render path that throws on real data. tests/test_dashboard.py covers the
 * structural contract; this script covers behaviour: it loads the real page into
 * jsdom, stubs the canvas, feeds it data fetched from a running monitor and
 * asserts what a person would see.
 *
 * Usage
 *   python -m network_monitor --host 127.0.0.1 --port 8000 &   # or the dev script
 *   npm install --no-save jsdom                                # not a runtime dep
 *   node scripts/dashboard-render-check.js
 *
 * Exit code 0 means every rendering path produced the expected content.
 */

"use strict";

const fs = require("fs");
const path = require("path");

const BASE_URL = process.env.LNM_BASE_URL || "http://127.0.0.1:8000";
const REPO = path.resolve(__dirname, "..");
const WEB = path.join(REPO, "src", "network_monitor", "web");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (error) {
  console.error(
    "jsdom is not installed. This is a development-only tool:\n" +
      "  npm install --no-save jsdom\n",
  );
  process.exit(2);
}

const failures = [];
const check = (label, condition, detail) => {
  if (!condition) failures.push(`${label}${detail ? ` — ${detail}` : ""}`);
};

async function fixtures() {
  const paths = {
    status: "api/status",
    ready: "api/ready",
    traffic: "api/traffic",
    interfaces: "api/interfaces",
    connections: "api/connections?limit=2000",
    history: "api/traffic/history?limit=900",
    events: "api/events?limit=100",
    system: "api/system",
  };
  const data = {};
  for (const [key, relative] of Object.entries(paths)) {
    const response = await fetch(`${BASE_URL}/${relative}`);
    if (!response.ok) throw new Error(`${relative} → HTTP ${response.status}`);
    data[key] = await response.json();
  }
  const events = data.events.events || [];
  if (events.length) {
    const worst = [...events].sort(
      (a, b) => ["info", "warning", "critical"].indexOf(a.severity) - ["info", "warning", "critical"].indexOf(b.severity),
    ).at(-1);
    const response = await fetch(`${BASE_URL}/api/events/${worst.id}`);
    data.event_detail = await response.json();
  } else {
    data.event_detail = null;
  }
  return data;
}

function fakeContext() {
  const gradient = { addColorStop() {} };
  return new Proxy(
    {},
    {
      get(target, prop) {
        if (prop === "createLinearGradient") return () => gradient;
        if (prop === "measureText") return () => ({ width: 10 });
        if (prop === "canvas") return { width: 640, height: 200 };
        return () => {};
      },
      set() {
        return true;
      },
    },
  );
}

async function main() {
  const data = await fixtures();
  const html = fs.readFileSync(path.join(WEB, "index.html"), "utf8");
  const script = fs.readFileSync(path.join(WEB, "js", "app.js"), "utf8");

  const dom = new JSDOM(html, { url: `${BASE_URL}/`, runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const { document } = window;
  window.HTMLCanvasElement.prototype.getContext = fakeContext;
  window.eval(script);

  const requested = [];
  window.fetch = async (requestedPath) => {
    requested.push(requestedPath);
    const payload =
      requestedPath.startsWith("api/events/") ? data.event_detail
      : requestedPath.startsWith("api/traffic/history") ? data.history
      : requestedPath.startsWith("api/traffic") ? data.traffic
      : requestedPath.startsWith("api/status") ? data.status
      : requestedPath.startsWith("api/ready") ? data.ready
      : requestedPath.startsWith("api/interfaces") ? data.interfaces
      : requestedPath.startsWith("api/connections") ? data.connections
      : requestedPath.startsWith("api/events") ? data.events
      : requestedPath.startsWith("api/system") ? data.system
      : undefined;
    if (payload === undefined) throw new Error(`unexpected request ${requestedPath}`);
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(payload)) };
  };

  const text = (id) => (document.getElementById(id)?.textContent || "").trim();

  document.dispatchEvent(new window.Event("DOMContentLoaded"));
  await new Promise((resolve) => setTimeout(resolve, 400));

  // 1 · right now
  check("verdict headline rendered", text("now-title").length > 0, text("now-title"));
  check("verdict tone set", ["ok", "work", "warn", "crit"].includes(document.getElementById("now-title").dataset.tone));
  check("download rate", /(B|KB|MB|GB)\/s/.test(text("metric-download")), text("metric-download"));
  check("download in Mb/s", text("metric-download-mbps").includes("Mb/s"), text("metric-download-mbps"));
  check("connections stat", /^\d+$/.test(text("metric-connections")), text("metric-connections"));
  check("events stat", /^\d+$/.test(text("metric-events")), text("metric-events"));
  check("uptime stat", text("metric-uptime") !== "—", text("metric-uptime"));
  check("readiness stat", ["Ready", "Not ready"].includes(text("metric-readiness")), text("metric-readiness"));

  // 2 · attention
  const attention = document.querySelectorAll("#attention-list li .event");
  const quiet = document.querySelectorAll("#events-body .event");
  if (attention.length) {
    check("attention rows explain why", [...attention].every((row) => (row.querySelector(".event-why")?.textContent || "").length > 10));
    check("attention empty state hidden", document.getElementById("attention-empty").hidden);
  } else {
    check("empty attention state shown", !document.getElementById("attention-empty").hidden);
  }
  check("info events folded into a disclosure", document.getElementById("info-events-disclosure") !== null);

  // 3 · trend
  check("chart caption states samples and window", /(samples|first samples)/.test(text("chart-note")), text("chart-note"));

  // 4 · who
  const processes = document.querySelectorAll("#process-list li.process");
  if ((data.connections.count || 0) > 0) {
    check("process ranking rendered", processes.length > 0, `rows=${processes.length}`);
    check("connection rows rendered", document.querySelectorAll("#connections-body tr").length > 0);
  }
  const filter = document.getElementById("filter-process");
  filter.value = "no-such-process-anywhere";
  filter.dispatchEvent(new window.Event("input"));
  check("local filter shows an empty state", document.querySelector("#connections-body .empty") !== null);
  filter.value = "";
  filter.dispatchEvent(new window.Event("input"));

  // 5 · where
  check("interface rows rendered", document.querySelectorAll("#interfaces-body tr").length > 0);

  // 6 · monitor
  check("collector rows rendered", document.querySelectorAll("#collectors-body tr").length > 0);
  check("storage rows rendered", document.querySelectorAll("#storage-list dt").length > 0);
  check("readiness checks rendered", document.querySelectorAll("#readiness-list dt").length > 0);
  check("host facts rendered", document.querySelectorAll("#host-list dt").length > 0);

  // interactions
  const before = document.documentElement.dataset.theme;
  document.getElementById("theme-toggle").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  check("theme toggle cycles", document.documentElement.dataset.theme !== before);

  if (attention.length && data.event_detail) {
    attention[0].dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 250));
    check("evidence dialog opens", document.getElementById("event-dialog").open === true);
    check("evidence rows labelled", document.querySelectorAll("#dialog-evidence dt").length > 0);
  }

  check("page reached ready state", document.body.dataset.state === "ready", document.body.dataset.state);
  check("no partial-failure banner on a healthy run", document.getElementById("error-banner").hidden, text("error-banner"));
  check("freshness reports live", document.getElementById("freshness").dataset.state === "fresh", text("freshness-text"));
  check("all panels requested", new Set(requested.map((p) => p.split("?")[0])).size >= 8);

  console.log(`dashboard render-check against ${BASE_URL}`);
  console.log(`  verdict      : ${text("now-title")} — ${text("now-detail").slice(0, 80)}`);
  console.log(`  attention    : ${attention.length} warnings/criticals, ${quiet.length} informational`);
  console.log(`  processes    : ${processes.length} ranked, ${text("connection-count")} connections`);
  console.log(`  chart        : ${text("chart-note").slice(0, 90)}`);

  if (failures.length) {
    console.error(`\nFAILURES (${failures.length}):`);
    failures.forEach((failure) => console.error(` - ${failure}`));
  } else {
    console.log("\nAll render checks passed.");
  }

  window.close();
  process.exit(failures.length ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
