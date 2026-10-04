# Local Network Monitor — MVP Specification and implementation status

**Document type:** Software Development MVP Specification / implementation record
**Version:** 1.0
**Platform:** Windows 10/11 (verified cross-platform on Linux)
**Language:** Python 3.12+ (runs on 3.11+)
**Architecture:** Modular monolith, single machine
**Primary storage:** SQLite · **API:** FastAPI · **UI:** local web dashboard

This document records the specification the MVP was built against and, for every
requirement, where it is implemented and how it is verified. Status values:

- **Done** — implemented and covered by tests
- **Done (adapted)** — implemented with a documented deviation
- **Deferred** — explicitly outside the MVP

---

## 1. Product definition

Build a Windows Local Network Monitor that continuously observes one machine's
network activity, measures usage, correlates connections with processes, detects
predefined abnormal conditions, stores history and presents it locally.

The five user questions and where each is answered:

| Question | Where |
|---|---|
| What is my computer doing on the network? | Overview card, `GET /api/status` |
| Which processes have network connections? | Processes & connections table, `GET /api/processes`, `GET /api/connections` |
| How much traffic is generated? | Traffic rates + chart, `GET /api/traffic`, `GET /api/traffic/history` |
| Is the network behaving abnormally? | Events feed, `GET /api/events`, six detection rules |
| What happened when it was detected? | Event detail dialog with structured evidence, `GET /api/events/{id}` |

---

## 2. Scope

### 2.1 Must have

| Requirement | Status | Where |
|---|---|---|
| Interface discovery | Done | `collectors/interface.py::list_interfaces`, `GET /api/interfaces` |
| Traffic metering | Done | `InterfaceCollector`, `slf::RateCalculator` |
| Historical measurements | Done | `storage/repositories.py::InterfaceMeasurementRepository` |
| Traffic rates (bytes/s) | Done | `RateCalculator.update` |
| TCP monitoring | Done | `collectors/connections.py::ConnectionCollector` |
| Process attribution | Done | `collectors/processes.py::ProcessResolver`, `attach_process_names` |
| Process information (name/exe/created) | Done | `ProcessInfo`, cached resolution |
| Network health (errors/drops) | Done | counters in `InterfaceMeasurement`, interface table column |
| Event detection | Done | `detection/rules.py` (6 rules) |
| Event storage | Done | `EventRepository` |
| REST API | Done | `api/routes.py` |
| Dashboard | Done | `web/` (vanilla HTML/CSS/JS) |
| Logging | Done | `logging_setup.py`, rotating `logs/monitor.log` |
| Resilience (one collector must not stop the monitor) | Done | `Monitor._safe_collect`, health registry, regression test |

### 2.2 Should have

| Requirement | Status | Where |
|---|---|---|
| Configurable thresholds | Done | `config.py::DetectionSection`, `config.yaml` |
| Event severity | Done | `EventSeverity` (info / warning / critical) |
| Collector health status | Done | `models/health.py`, `/api/status`, dashboard strip |
| Basic traffic history | Done | `/api/traffic/history`, 15-minute chart |
| Connection filtering | Done | `/api/connections?process=&pid=&state=&remote=` |
| Process filtering | Done | dashboard filter inputs, `/api/processes` |
| Exportable measurements | Done (adapted) | JSON over the API (`/api/traffic/history`); no CSV/Excel export |

### 2.3 Later — not in this MVP

Packet capture · DPI · ML anomaly detection · exact per-process byte accounting ·
multi-machine monitoring · cloud deployment · authentication · PostgreSQL ·
Redis · Kafka · Kubernetes · microservices · containerisation · advanced threat
detection. None of these are implemented, and the architecture leaves room for
them without changing the collector contract.

---

## 3. Architecture

The implementation follows the specified pipeline exactly:

```
COLLECT -> NORMALIZE -> STORE -> DETECT -> EXPLAIN -> NOTIFY -> DISPLAY
```

| Stage | Implementation |
|---|---|
| COLLECT | `collectors/{interface,connections,processes,system}.py` |
| NORMALIZE | `models/network.py`, `models/process.py`, normalisers in the collectors |
| STORE | `storage/database.py`, `storage/repositories.py` (SQLite/WAL) |
| DETECT | `detection/engine.py` + `detection/rules.py` |
| EXPLAIN | `Event.description` + structured `Event.evidence` |
| NOTIFY | `notifications/manager.py` (policy) → `notifications/email.py` (transport) |
| DISPLAY | `api/` (FastAPI) + `web/` (dashboard) |

### The important technical boundary

Interface metering (`psutil.net_io_counters` → bytes/s) and connection
attribution (TCP table → PID → process) are kept separate and are never mixed
into a per-process byte claim. See README §1. This is enforced by design: no
code path multiplies a connection count by an interface rate.

---

## 4. Technology stack

| Concern | Specified | Implemented |
|---|---|---|
| Interface telemetry | `psutil.net_io_counters(pernic=True)` | Yes, `collectors/interface.py` |
| Rate calculation | `(current - previous)/elapsed`, reset-safe | Yes, `RateCalculator`, unit-tested |
| TCP connections | `Get-NetTCPConnection` | Yes (primary on Windows), with `psutil` and `netstat -ano` fallbacks |
| Future connection source | `GetExtendedTcpTable()` | Deferred; collection sources are injectable |
| Process information | `psutil.Process(pid)` | Yes, with cache and full error taxonomy |
| Performance counters | "later" | Deferred; `psutil` is used, as specified for the MVP |

### Counter-reset handling (Milestone 1 acceptance criterion)

```
current < previous  ->  discard the negative delta
                    ->  report 0 B/s for that interval
                    ->  establish a new baseline
```

Implemented in `RateCalculator.update`; asserted by
`tests/test_metering.py::TestRateCalculator::test_counter_reset_is_discarded_not_negative`
and `::TestInterfaceCollector::test_counter_reset_never_yields_a_negative_rate`.

---

## 5. Data model

| Entity | Table | Notes |
|---|---|---|
| `InterfaceMeasurement` | `interface_measurements` | counters + `upload_rate`/`download_rate` |
| `NetworkConnection` | `connections` | protocol, endpoints, state, pid, process name |
| `Process` | `processes` | pid (PK), name, executable, created_at, last_seen |
| `Event` | `events` | type, severity, title, description, source, pid, process, interface, evidence (JSON), status |
| Collector health | `collector_health` | state, last success/attempt, last error |

Timestamps are ISO-8601 UTC with millisecond precision. Rates are bytes/second.

---

## 6. Detection rules

| # | Rule | Event type | Severity | Notes |
|---|---|---|---|---|
| 1 | `download_rate > threshold` | `HIGH_DOWNLOAD` | warning | threshold in Mb/s, compared in bytes/s |
| 2 | `upload_rate > threshold` | `HIGH_UPLOAD` | warning | |
| 3 | error counter delta > 0 | `INTERFACE_ERROR` | warning | **delta**, not the cumulative counter |
| 4 | unseen PID opens a connection | `NEW_NETWORK_PROCESS` | info | observation, seeded at startup |
| 5 | connections > baseline × multiplier | `CONNECTION_SPIKE` | warning | baseline = mean of last 120 cycles, minimum baseline guard |
| 6 | no successful collection within timeout | `COLLECTOR_FAILURE` | critical | per collector |

Specification acceptance criterion — `download_rate = 20 MB/s` with a
`10 MB/s` threshold produces `HIGH_DOWNLOAD` — is asserted verbatim by
`tests/test_detection.py::TestHighDownloadRule::test_acceptance_criterion_20_mbps_above_10_mbps_threshold`.

Engine-level protections:

- **Cooldown** (`event_cooldown_seconds`): a sustained condition produces one
  event, not one per second.
- **A broken rule cannot stop the others** (each rule runs in its own
  try/except; regression-tested).
- **Process baseline**: `seed_processes_on_start` prevents a startup flood.

---

## 7. API specification

Base URL `http://127.0.0.1:8000`. All endpoints below are implemented and
covered by `tests/test_api.py`.

| Specified | Status |
|---|---|
| `GET /api/status` | Done — includes collectors, detection, notifications, storage |
| `GET /api/interfaces` | Done |
| `GET /api/traffic` | Done |
| `GET /api/connections` | Done (+ filters) |
| `GET /api/processes` | Done |
| `GET /api/traffic/history?from=&to=&interface=` | Done (+ `limit`) |
| `GET /api/events` | Done (+ severity/type/status filters, paging) |
| `GET /api/events/{id}` | Done — evidence included |

Additional, non-specified endpoints: `GET /api/health`, `GET /api/system`,
`GET /api/info`, `PATCH /api/events/{id}`, OpenAPI at `/api/openapi.json`,
Swagger UI at `/api/docs`.

---

## 8. Dashboard

| Specified section | Implemented as |
|---|---|
| A. Overview (status, download, upload, active TCP, events) | "Network status" card + collector health strip |
| B. Interfaces (per-interface rates) | "Interfaces" table with rates, totals and error counts |
| C. Processes / connections (process, PID, remote, state) | "Processes & connections" table with filters |
| D. Events (time, severity, event) | "Events" feed; click for full evidence |

Plus a traffic history chart. Refreshed every 2 s by default, no external assets.

---

## 9. Runtime loop

```
initialize config → initialize database → start API → start collectors
   → [measure] → [attribute] → [store] → [detect] → [explain] → [notify] → [display] → repeat
```

Implemented in `Monitor.run_forever` / `Monitor.cycle`, default interval 1.0 s
(configurable, minimum 0.1 s). Retention pruning runs at most once per
`prune_interval_seconds`.

---

## 10. Testing strategy

| Level | Specified | Implemented |
|---|---|---|
| Unit | rate calculation, counter reset, thresholds, events, normalisation | `test_metering.py`, `test_detection.py` |
| Collector | valid/empty/malformed output, command failure, missing PID, terminated process | `test_collectors.py` |
| Integration | collector → normaliser → database → detection → event | `test_integration.py` |
| API | all endpoints return 200 | `test_api.py` |

Run with `python -m pytest` (157 tests).

---

## 11. Non-functional requirements

| Requirement | Status | Evidence |
|---|---|---|
| Reliability: a collector failure must not stop the monitor | Done | `Monitor._safe_collect`, `test_monitor.py::TestResilience` |
| Performance: low overhead | Done | 1 s interval, snapshot de-duplication, cached process lookups, indexed queries |
| Data integrity: timestamps + consistent units | Done | UTF-8 ISO-8601 UTC, bytes/s everywhere |
| Observability: collector status, last success, errors, logs | Done | `/api/status`, collector health table, rotating log file |
| Maintainability: independently testable layers | Done | collectors/storage/detection/API/notifications separated; 157 tests |
| Security: bind locally, validate subprocess args, no arbitrary execution, minimal data | Done | loopback default, fixed argument lists, `shell=False`, timeouts, no payload capture |

---

## 12. Configuration

See README §7 for the annotated file. Configuration is centralised in
`config.py`; unknown keys are rejected by `--check-config` rather than ignored.

---

## 13. Definition of Done — status

### Runtime
- [x] Application runs on Windows (primary target; also runs on Linux/macOS)
- [x] Starts without manual database setup
- [x] Continuously collects network data
- [x] Survives individual collector failures

### Monitoring
- [x] Interfaces detected
- [x] Upload/download measured
- [x] Rates calculated correctly (incl. counter resets)
- [x] TCP connections collected
- [x] Owning PIDs identified
- [x] PIDs resolved to processes

### Storage
- [x] Measurements persisted in SQLite
- [x] Events persisted
- [x] History survives restart (persistence tests reopen the file)

### Detection
- [x] High traffic detected
- [x] Interface errors detected
- [x] New network processes identified
- [x] Connection spikes detected
- [x] Collector failures recorded

### API
- [x] FastAPI running locally
- [x] Status endpoint works
- [x] Traffic endpoint works
- [x] Connection endpoint works
- [x] Process endpoint works
- [x] Event endpoint works

### UI
- [x] Current network status visible
- [x] Traffic visible (+ history chart)
- [x] Connections visible
- [x] Processes visible
- [x] Events visible

### Quality
- [x] Unit tests
- [x] Integration tests
- [x] API tests
- [x] Logging works
- [x] README contains installation and usage instructions

---

## 14. Backlog mapping (LNM-001 … LNM-025)

| Item | Deliverable | Status |
|---|---|---|
| LNM-001 | Repository & project setup | Done — `pyproject.toml`, `.gitignore`, MIT-ready layout |
| LNM-002 | Python environment | Done — `requirements.txt`, `requirements-dev.txt`, venv docs |
| LNM-003 | Application entry point | Done — `main.py`, `__main__.py`, console script |
| LNM-004 | Configuration system | Done — `config.py`, `config.yaml`, `--check-config` |
| LNM-005 | Logging | Done — `logging_setup.py` (rotating file + console) |
| LNM-006 | Interface collector | Done |
| LNM-007 | Traffic rate calculator | Done |
| LNM-008 | Rate-calculation tests | Done |
| LNM-009 | SQLite database | Done — schema v1, WAL |
| LNM-010 | Measurement repository | Done (+ connection/process/event/health repositories) |
| LNM-011 | TCP connection collector | Done — PowerShell, psutil, netstat |
| LNM-012 | Process resolver | Done — cache + error taxonomy |
| LNM-013 | FastAPI application | Done |
| LNM-014 | Traffic API | Done |
| LNM-015 | Connection API | Done |
| LNM-016 | Process API | Done |
| LNM-017 | Detection engine | Done |
| LNM-018 | Detection rules | Done — all six |
| LNM-019 | Event system | Done — evidence + status lifecycle |
| LNM-020 | Collector health monitoring | Done — registry + persisted table |
| LNM-021 | Notification manager | Done — policy, cooldown, async queue |
| LNM-022 | Email notifier | Done — SMTP, non-blocking |
| LNM-023 | Dashboard | Done — four sections + chart |
| LNM-024 | Integration tests | Done |
| LNM-025 | MVP hardening | Done — retention, de-duplication, validation, error handling |

### Recommended Git structure

The specification suggested one feature branch per increment merging into `main`.
Implementation was delivered here as a single reviewed work branch
(`arena/01a107e1-local-network-monitor`) intended to merge into `main` via a pull
request; the commit history is layered so each milestone's change set can be
reviewed independently.

---

## 15. Deviations from the specification (all documented, none silent)

| # | Deviation | Reason |
|---|---|---|
| 1 | `MonitorState` gained an in-memory "live snapshot" that the API prefers while the loop is running | Otherwise an honestly empty result (zero connections right now) would be replaced by stale rows read back from SQLite |
| 2 | Connection snapshots are only written when the connection set changes, and are pruned on `connection_retention_hours` (24 h) rather than `retention_days` | Prevents unbounded growth of the most voluminous table with no loss of information |
| 3 | `INTERFACE_ERROR` compares counter **deltas** | Cumulative counters would otherwise raise an event every cycle forever |
| 4 | `/api/system` and `PATCH /api/events/{id}` added; OpenAPI moved to `/api/openapi.json` (UI at `/api/docs`) | Small, clearly useful additions; `/docs` is left to the UI's own layout |
| 5 | `SystemSnapshot.connection_states` is filled from the connections already collected | Avoids walking the socket table twice per cycle |
| 6 | `--once`, `--api-only`, `--check-config` CLI flags added | Make the tool verifiable and testable without a long-running server |
| 7 | "Exportable measurements" is served as JSON over the API | CSV was not specified concretely; the API already exposes the raw series |

---

## 16. Final boundary

> **Do not build a sophisticated network-security platform. Build a reliable
> local measurement and explanation system first.**

The first vertical slice is complete end to end:

```
interface counters → rate calculation → SQLite → FastAPI → /api/traffic → dashboard
```

…and the specified follow-on chain is in place in order:
connections → processes → detection → events → notifications.
