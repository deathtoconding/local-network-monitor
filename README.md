# Local Network Monitor

A local, single-machine network monitor for **Windows 10/11** (and any other
platform Python runs on). It continuously measures interface traffic, correlates
active TCP connections with the processes that own them, applies a small set of
**deterministic** detection rules, stores everything in SQLite, and shows it in
a local web dashboard.

[![CI](https://github.com/deathtoconding/local-network-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/deathtoconding/local-network-monitor/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20Linux%20%7C%20macOS-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

**Documentation:** [Architecture](docs/ARCHITECTURE.md) ·
[SLOs](docs/SLO.md) · [Runbook](docs/RUNBOOK.md) · [Plan](docs/PLAN.md) ·
[Specification](docs/SPECIFICATION.md) · [Decisions](docs/adr/README.md) ·
[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

It answers five questions:

1. What is my computer doing on the network?
2. Which processes currently have network connections?
3. How much network traffic is being generated?
4. Is the network behaving abnormally?
5. What happened when an abnormal condition was detected?

Design principle, straight from the specification:

> **Do not build a sophisticated network-security platform. Build a reliable local
> measurement and explanation system first.**

Everything runs locally. There is no cloud component, no authentication layer,
no packet capture and no machine learning. The API binds to `127.0.0.1` by
default.

---

## 1. The one technical boundary that matters

The monitor is strict about the difference between two kinds of data:

| | Question it answers | Where it comes from |
|---|---|---|
| **Interface metering** | "How much traffic?" | Cumulative per-interface byte counters (`psutil.net_io_counters`), differentiated into bytes/second |
| **Connection attribution** | "Who is talking?" | TCP table + owning PID (`Get-NetTCPConnection`), resolved to a process name |

```
Ethernet:  Download 18.4 MB/s   Upload 2.1 MB/s      <- measured per interface
chrome.exe PID 8420 -> 142.250.x.x:443 ESTABLISHED   <- attributed per connection
```

These are **not the same measurement**. The dashboard and API never claim that
Chrome transferred exactly X MB, because this design has no reliable
per-process byte accounting. Adding that claim would mean adding a different
mechanism (ETW / per-socket counters), not a different query.

---

## 2. Requirements

- Python **3.11+** (developed against 3.12; the target platforms are Windows 10/11)
- PowerShell (built into Windows) for the primary connection collector — the
  monitor degrades gracefully to `psutil` and `netstat` when unavailable
- No database server: SQLite is part of the Python standard library

## 3. Install

### Windows (PowerShell)

```powershell
git clone https://github.com/deathtoconding/local-network-monitor.git
cd local-network-monitor

py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For a development install (adds `pytest` and gives you the `network-monitor`
console script):

```powershell
python -m pip install -e ".[dev]"
```

### Linux / macOS

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
```

> On Linux and macOS, seeing *which* process owns a socket requires
> `CAP_NET_ADMIN` / root; without it the connection table still works but
> process names show up as `<access_denied>` or `unknown`. This limitation does
> not exist on Windows.

## 4. Run

```powershell
# Dashboard + API on http://127.0.0.1:8000
python -m network_monitor

# One collection cycle, printed to the console, then exit (good first check)
python -m network_monitor --once

# Serve the dashboard over data collected by an earlier run, no collection
python -m network_monitor --api-only

# Validate the configuration file without starting anything
python -m network_monitor --check-config
```

Expected banner:

```
====================================================
 Local Network Monitor v0.1.0
====================================================
 Status:       RUNNING
 Dashboard:    http://127.0.0.1:8000
 API docs:     http://127.0.0.1:8000/api/docs
 Interval:     1.0s
 Database:     data/monitor.db
 Log file:     logs/monitor.log
 Config:       /path/to/config.yaml
====================================================
```

Open <http://127.0.0.1:8000> for the dashboard and
<http://127.0.0.1:8000/api/docs> for the interactive API reference (Swagger UI).

Installing the package gives you the same thing as a console script:

```powershell
python -m pip install .          # or: pip install -e ".[dev]" for development
network-monitor --once
```

The dashboard ships inside the package, so an installed wheel is a complete
product — the CI packaging job starts the installed wheel and fetches the real
page to prove it.

### Command-line options

| Flag | Meaning |
|---|---|
| `--config PATH` | YAML configuration file (default: `./config.yaml` when present) |
| `--host` / `--port` | Dashboard bind address and port (default `127.0.0.1:8000`) |
| `--interval` | Seconds between collection cycles (default `1.0`) |
| `--api-only` | Serve the API/dashboard without starting collectors |
| `--once` | Run a single cycle, print a summary, exit |
| `--check-config` | Validate configuration and exit |
| `--log-level` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `--version` | Print the version and exit |

The database and its schema are created automatically on first start. Nothing
has to be set up by hand, and restarting the monitor never deletes history.

## 5. Dashboard

Four sections, refreshed every 2 seconds (1 s / 5 s / paused selectable):

- **Network status** — health, download/upload rate, active TCP connections,
  24-hour event count, uptime, and a per-collector health strip
- **Traffic history** — canvas chart of the last 15 minutes, download vs upload
- **Interfaces** — per-interface rates, cumulative bytes, error/drop counters
- **Processes & connections** — process name, PID, local and remote endpoints,
  TCP state, with process / remote / state filters
- **Events** — severity-coloured feed; click any event for its full evidence

A configurable table, no build step, no CDN: the dashboard is plain HTML/CSS/JS
served by the same FastAPI process as the API, so it works on a machine with no
internet access.

## 6. API

Base URL `http://127.0.0.1:8000`. All timestamps are ISO-8601 UTC; all rates are
bytes/second (megabit values are provided alongside for readability).

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Overall status, uptime, collector health, rule catalogue, storage counts |
| `GET` | `/api/health` | Liveness probe |
| `GET` | `/api/interfaces` | Detected interfaces with current rates and cumulative counters |
| `GET` | `/api/traffic` | Current aggregate download/upload rate |
| `GET` | `/api/traffic/history?from=&to=&interface=&limit=` | Historical rate series |
| `GET` | `/api/connections?process=&pid=&state=&remote=&limit=` | Latest TCP snapshot with owner resolution |
| `GET` | `/api/processes?limit=` | Processes owning network connections, with connection counts |
| `GET` | `/api/events?severity=&event_type=&status=&from=&to=&limit=&offset=` | Detected events, newest first |
| `GET` | `/api/events/{id}` | One event including its structured evidence |
| `PATCH` | `/api/events/{id}` | Set event status (`open` / `acknowledged` / `resolved`) |
| `GET` | `/api/system` | Host metrics and storage row counts |
| `GET` | `/api/ready` | Depth probe: database writable, loop fresh, last cycle clean (503 when not ready) |
| `GET` | `/api/metrics` | Prometheus text exposition for scraping |

Example:

```console
$ curl -s http://127.0.0.1:8000/api/traffic
{
  "timestamp": "2026-10-04T17:40:20.520+00:00",
  "download_bytes_per_second": 18400000.0,
  "upload_bytes_per_second": 2100000.0,
  "download_mbps": 147.2,
  "upload_mbps": 16.8,
  "interfaces": { "Ethernet": { "...": "..." } }
}

$ curl -s http://127.0.0.1:8000/api/events/1
{
  "id": 1,
  "event_type": "HIGH_DOWNLOAD",
  "severity": "warning",
  "title": "High download traffic on Ethernet",
  "description": "Download traffic reached 20.0 Mb/s, above the configured threshold of 10.0 Mb/s.",
  "evidence": {
    "interface": "Ethernet",
    "download_rate_bytes_per_second": 2500000,
    "download_rate_mbps": 20.0,
    "threshold_mbps": 10.0
  }
}
```

## 7. Configuration

Every value has a built-in default; `config.yaml` only overrides what you change.

```yaml
monitor:
  collection_interval: 1.0        # seconds; 1s is the recommended minimum
  host: 127.0.0.1                 # keep on loopback unless you know why
  port: 8000
  exclude_interfaces: ["Loopback Pseudo-Interface 1", "lo"]

database:
  path: data/monitor.db
  retention_days: 7               # measurements + events
  connection_retention_hours: 24  # connection snapshots (far more numerous)
  prune_interval_seconds: 3600

detection:
  download_threshold_mbps: 10     # 0 disables the rule
  upload_threshold_mbps: 5
  connection_spike_multiplier: 3.0
  connection_spike_min_baseline: 10
  collector_failure_timeout_seconds: 15
  event_cooldown_seconds: 60      # identical events are not repeated inside this window
  seed_processes_on_start: true   # avoid a NEW_NETWORK_PROCESS storm at startup
  new_process_rule_enabled: true

notifications:
  enabled: false
  min_severity: warning
  cooldown_seconds: 300
  email:
    enabled: false
    smtp_host: smtp.example.com
    smtp_port: 587
    use_tls: true
    username: ""
    password: ""                  # prefer the LNM_SMTP_PASSWORD environment variable
    sender: monitor@example.com
    recipients: [admin@example.com]

logging:
  level: INFO
  format: text                    # text for humans, json for log shippers
  file: logs/monitor.log
  max_bytes: 5242880
  backup_count: 3
  console: true
```

Unknown keys and out-of-range values are rejected with a clear message by
`--check-config`, so typos never silently disable a rule.

## 8. Detection rules

Deterministic rules only — no AI, no ML, no heuristics that cannot be explained.

| Rule | Event type | Severity | Trigger |
|---|---|---|---|
| 1 | `HIGH_DOWNLOAD` | warning | `download_rate > download_threshold_mbps` |
| 2 | `HIGH_UPLOAD` | warning | `upload_rate > upload_threshold_mbps` |
| 3 | `INTERFACE_ERROR` | warning | interface error/drop counters increased since the previous cycle |
| 4 | `NEW_NETWORK_PROCESS` | info | a PID that had not been seen using the network opens a connection |
| 5 | `CONNECTION_SPIKE` | warning | `connections > rolling_baseline × multiplier` (baseline = mean of the last 120 cycles) |
| 6 | `COLLECTOR_FAILURE` | critical | a collector has not succeeded within `collector_failure_timeout_seconds` |

Two deliberate details:

- **Rule 3 uses deltas, not absolute counters.** Interface error counters are
  cumulative since boot; without the delta a machine that saw one error last
  month would raise an event every second forever.
- **Rule 4 is an observation, not an accusation.** The wording says so, and the
  evidence records the executable path and remote endpoints so a human can
  judge. `seed_processes_on_start` marks already-running network processes as
  known so the first cycle is quiet.

Every event carries: what, when, where (interface / PID / process), how severe,
why it fired and the structured evidence behind it.

## 9. Architecture

```
COLLECT -> NORMALIZE -> STORE -> DETECT -> EXPLAIN -> NOTIFY -> DISPLAY
```

```
             Windows machine (single process, modular monolith)
                              |
        +---------------------+---------------------+
        v                                           v
 Interface collector                        Connection collector
 psutil.net_io_counters                     Get-NetTCPConnection (JSON)
 cumulative counters                        -> psutil -> netstat -ano
        |                                           |
        v                                           v
   Rate calculator                            Process resolver
   delta/elapsed, reset-safe                  psutil.Process, cached, fault tolerant
        |                                           |
        +---------------------+---------------------+
                              v
                     Measurement layer (models)
                              |
              +---------------+---------------+
              v                               v
           SQLite                       Detection engine
      measurements, connections,      6 deterministic rules,
      processes, events, health       cooldown + rolling baseline
              |                               |
              |                               v
              |                             Events (with evidence)
              +---------------+---------------+
                              v
                           FastAPI  ──  REST + static dashboard
                              |
                 +------------+------------+
                 v                         v
          Web dashboard            Notification manager
          (/, vanilla JS)          -> email notifier (async worker)
```

Each stage has one responsibility. Collectors know nothing about storage;
detection knows nothing about HTTP; notifiers know nothing about rules.

### Project layout

```
local-network-monitor/
├── src/network_monitor/
│   ├── main.py              # CLI entry point, banner, uvicorn wiring
│   ├── monitor.py           # the runtime loop (COLLECT..DISPLAY)
│   ├── config.py            # layered configuration (defaults <- YAML)
│   ├── logging_setup.py     # rotating file + console logging (text or JSON)
│   ├── collectors/
│   │   ├── base.py          # Collector contract + CollectorError
│   │   ├── interface.py     # counters + RateCalculator
│   │   ├── connections.py   # PowerShell / psutil / netstat sources
│   │   ├── processes.py     # PID -> process name/executable, cached
│   │   └── system.py        # host metrics
│   ├── models/              # network.py, process.py, events.py, health.py
│   ├── storage/             # database.py (SQLite), repositories.py, schema.py
│   ├── detection/           # rules.py, engine.py
│   ├── api/                 # app.py, routes.py, state.py, metrics.py
│   ├── notifications/       # manager.py, email.py
│   └── web/                 # dashboard assets, shipped inside the package
├── tests/                   # 251 tests: unit, collector, integration, API, chaos
├── docs/                    # architecture, SLOs, runbook, plan, ADRs, spec
├── .github/                 # CI, CodeQL, release workflow, templates
├── config.yaml              # documented defaults
└── requirements*.txt, pyproject.toml, Makefile, scripts/
```

The dashboard lives inside the package (`src/network_monitor/web/`) rather than
at the repository root, so `pip install` produces a complete product instead of
an API with a missing page.`

### Collector contract, and what "resilience" means here

```python
class Collector:
    def collect(self): ...   # return data, or raise CollectorError
```

The monitoring loop wraps every collector call: a failure is recorded in the
collector health registry (state, last success, last error, consecutive
failures), logged, and reflected in `/api/status` and the dashboard's health
strip. After `collector_failure_timeout_seconds` it also raises a
`COLLECTOR_FAILURE` event. **A dead PowerShell does not stop interface
metering** — that is covered by an explicit regression test.

### Storage

SQLite, WAL mode, schema created on first start (`PRAGMA user_version` is set so
migrations have a home). Tables: `interface_measurements`, `connections`,
`processes`, `events`, `collector_health`.

Connection snapshots are written **only when the connection set changes**, so an
idle machine writes almost nothing; identical sets are not re-inserted every
second. They are also pruned on a shorter window than measurements, because they
are far more numerous.

## 10. Notifications

```
Detection rule -> Event -> Notification manager -> Notifier(s) -> Email
```

The manager owns policy (enabled, `min_severity`, per-event-type cooldown) and
hands delivery to a background worker thread fed by a bounded queue. A slow or
unreachable SMTP server therefore cannot block the monitoring loop. Email
delivery is off by default; configure `notifications.email.*` and set
`LNM_SMTP_PASSWORD` in the environment rather than a password in the YAML file.

## 11. Operability

The monitor is meant to be run for weeks, so it carries its own operations
surface rather than expecting someone to watch it:

| Need | Where |
|---|---|
| "Is it healthy right now?" | `GET /api/status` — collector states, SLIs, `last_errors` |
| "Is it safe to depend on?" | `GET /api/ready` — 503 unless the database is writable, the loop is fresh **and** the last cycle was clean |
| "Show me in my monitoring tool" | `GET /api/metrics` — Prometheus text, bounded cardinality, no client library |
| "What happened at 03:00?" | `GET /api/events` + `logs/monitor.log` (JSON layout optional) |
| "What do I do about X?" | [RUNBOOK.md](docs/RUNBOOK.md) |
| "How good is good enough?" | [SLO.md](docs/SLO.md) — targets, error budget, alert expressions |
| "Why is it built like this?" | [ARCHITECTURE.md](docs/ARCHITECTURE.md) + [ADRs](docs/adr/README.md) |

```powershell
curl -s http://127.0.0.1:8000/api/ready
# {"ready":true,"checks":{"collection_errors":true,"collection_loop":true,"database":true,"notifications":true},...}

curl -s http://127.0.0.1:8000/api/metrics | Select-String lnm_collector_up
# lnm_collector_up{collector="interface"} 1
# lnm_collector_up{collector="connections"} 1
```

A stalled loop, an unwritable database, or a collector that failed on the last
cycle all turn `/api/ready` into a 503 — the probe deliberately refuses to report
"ready" while serving data it cannot vouch for.

## 12. Tests

```bash
python -m pytest              # 251 tests, ~2 s
python -m pytest -k rate      # rate calculation only
```

Levels, matching the specification:

- **Unit** — rate calculation (including counter resets), threshold rules, event
  creation, data normalisation
- **Collector** — valid / empty / malformed Windows output, command failure,
  missing PID, terminated process, access denied
- **Integration** — collector → normaliser → SQLite → detection → event → API
- **API** — every documented endpoint returns 200 with valid JSON, plus filters,
  404s and 422 validation
- **Fault injection** (`test_fault_injection.py`) — storage failures, dead
  collectors, an exploding detection rule and an unreachable SMTP server: the loop
  must survive all of them and say so
- **Observability** (`test_observability.py`) — metrics parse, readiness
  semantics, JSON log shape

CI runs the matrix on **Windows and Linux, Python 3.11 and 3.12**, with an 85 %
coverage floor (currently 88 %).

## 13. Windows notes

- **Connection collector priority:** `Get-NetTCPConnection` (gives the owning
  PID directly) → `psutil.net_connections` → `netstat -ano`. All three produce
  the same normalised model, so the rest of the system never learns which one
  answered; `/api/status` records where the data came from.
- **Subprocesses are never invoked through a shell**, arguments are fixed, and
  every call has a 10-second timeout. No user input reaches a command line.
- **Elevation is not required** for interface metering. It helps for complete
  process attribution of other users' sockets; without it you may see
  `<access_denied>` entries, which are reported honestly rather than guessed.
- **Collection interval:** keep it at ≥ 1 second. Windows performance counters
  are not designed for faster polling, and the monitor is meant to be quiet.
- The collector design leaves room for `GetExtendedTcpTable()` (IP Helper API)
  and Windows Performance Counters as future sources; the model and the loop
  would not change.

## 14. Security posture

- Binds to `127.0.0.1` by default; use `--host 0.0.0.0` only deliberately
- No authentication in the MVP (single-user local tool) — so never expose it
  off-host
- Read-only view of the host: the monitor never blocks traffic, never modifies
  firewall or network state
- No packet payload is read or stored; only endpoint metadata and process names
- SMTP credentials come from the environment, not the repository

## 15. Roadmap (explicitly not in the MVP)

Packet capture, deep packet inspection, ML anomaly detection, exact per-process
byte accounting, multi-machine monitoring, cloud deployment, authentication,
PostgreSQL, Redis, Kafka, containerisation. See
[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) for the full scope boundary.

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Connection collector` shows `access_denied` | Not elevated (Linux/macOS), or PowerShell blocked by policy. Interface metering still works. |
| Process names show `unknown` | The socket table does not expose owners on this platform/session; run elevated. |
| No `NEW_NETWORK_PROCESS` events | `detection.new_process_rule_enabled` is off, or the PIDs were seeded at startup. |
| `COLLECTOR_FAILURE` for `connections` | `Get-NetTCPConnection` is slow or blocked; check `logs/monitor.log` and `consecutive_failures`. |
| Rates look like 0 for the first second | The first cycle only establishes the baseline; rates need two samples. |
| Dashboard reachable but empty | The monitor has not completed a cycle yet, or `--api-only` is serving an empty database. |
| `/api/ready` returns 503 | Read `checks` in the response: `database`, `collection_loop` or `collection_errors` names the failing area. |
| `/api/metrics` empty in Grafana | The scrape path is relative; some agents need the full URL including `/api/metrics`. |

## 17. License

MIT. See [LICENSE](LICENSE).
