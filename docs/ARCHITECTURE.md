# Architecture

**Audience:** contributors, reviewers, and anyone operating the monitor who needs
to reason about failure modes rather than file names.

Related documents: [SPECIFICATION.md](SPECIFICATION.md) (scope and requirements) ·
[SLO.md](SLO.md) (what "healthy" means numerically) · [RUNBOOK.md](RUNBOOK.md)
(what to do when it is not) · [adr/](adr/README.md) (why it is built this way).

---

## 1. Context

One machine, one process, one user. The monitor observes the host it runs on and
never acts on it: it does not block traffic, change firewall state, or read
packet payloads.

```
                    ┌───────────────────────────────┐
                    │  Windows host (single machine) │
                    │                               │
   OS kernel ──────►│  local-network-monitor        │
   (counters,       │  ┌─────────────────────────┐  │
    TCP table,      │  │ collector loop (1 Hz)   │  │
    process table)  │  └──────────┬──────────────┘  │
                    │             │                 │
                    │        ┌────▼────┐            │
                    │        │ SQLite  │            │
                    │        └────┬────┘            │
                    │             │                 │
   operator ───────►│   FastAPI ──┴── dashboard      │
   (browser)        └───────────────────────────────┘
                                 │
                                 └──► SMTP (optional, outbound only)
```

**Explicit non-goals:** multi-machine aggregation, cloud deployment,
authentication, packet capture, deep packet inspection, machine learning,
containerisation. See [SPECIFICATION.md §2.3](SPECIFICATION.md).

---

## 2. Component responsibilities

| Component | Module | Owns | Must never |
|---|---|---|---|
| Interface collector | `collectors/interface.py` | Cumulative counters → measurements; rate baselines | Touch the database |
| Rate calculator | `collectors/interface.py::RateCalculator` | delta/elapsed arithmetic, counter-reset policy | Know about psutil or wall-clock |
| Connection collector | `collectors/connections.py` | TCP table acquisition across three sources, parsing, normalisation | Resolve process names |
| Process resolver | `collectors/processes.py` | PID → name/exe/created, caching, error taxonomy | Guess a name it could not read |
| System collector | `collectors/system.py` | Host metrics for context | Walk the socket table (the connection collector already did) |
| Repositories | `storage/repositories.py` | Every SQL statement | Contain business rules |
| Detection engine | `detection/engine.py` | Baseline, seen-PID set, cooldown, rule execution | Store, notify, or log to HTTP |
| Rules | `detection/rules.py` | Pure functions: context in, events out | Read the clock, the database, or the network |
| Monitor loop | `monitor.py` | Cycle orchestration, failure isolation, retention | Know about HTTP |
| API | `api/` | Read models, filtering, serialisation, static dashboard | Collect anything |
| Notifications | `notifications/` | Policy (manager) and transport (notifier) | Run inside the collection path |
| CLI | `main.py` | Process lifecycle, wiring, human output | Contain domain logic |

The dependency direction is strictly one-way: `main → monitor → {collectors,
storage, detection, notifications} → models`, with `api → {storage, state,
models}`. No cycles, no module reaching "upward".

---

## 3. One cycle, end to end

```
                          Monitor.cycle()
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 InterfaceCollector      ConnectionCollector      ProcessResolver
 (psutil counters)       (Get-NetTCPConnection     (psutil.Process,
        │                 → psutil → netstat)        cached 5 s)
        │                       │                       │
        └───────────┬───────────┴───────────────────────┘
                    ▼
          NORMALIZE: normalise endpoints, attach process names
                    │
                    ▼
          STORE: measurements (always), connection snapshot
                 (only when the set changed), process directory,
                 collector health
                    │
                    ▼
          DETECT: DetectionEngine.evaluate(...)
                  6 rules → candidate events → cooldown filter
                    │
                    ▼
          EXPLAIN: Event{title, description, evidence{...}}
                    │
                    ├──────────────► EventRepository (persist)
                    │
                    ├──────────────► NotificationManager.handle_events()
                    │                       └── queue ──► worker thread ──► SMTP
                    ▼
          DISPLAY: MonitorState.publish_cycle()
                   (in-memory snapshot read by the API, no request-path SQL)
```

Timing: the loop targets `collection_interval` (default 1 s) and subtracts the
cycle's own duration from the sleep, so a slow cycle does not accumulate drift.
Measured cycle duration on a Linux sandbox with ~60 connections is 3–15 ms; a
Windows host with several hundred connections and PowerShell in the path is
expected in the 50–300 ms range (see `/api/status.last_cycle_duration_ms`).

---

## 4. Concurrency model

| Thread | Started by | Work | Blocking it would cause |
|---|---|---|---|
| Main | `uvicorn.Server.run()` | HTTP, waits on the event loop | Everything |
| `monitor-loop` | `Monitor.start()` | Collection, storage, detection | Stale dashboard, readiness failure |
| `notifications` | `NotificationManager.start()` | SMTP delivery from a bounded queue | Nothing (by construction) |

Shared state and how it is protected:

- **SQLite** — one connection, `check_same_thread=False`, WAL mode, serialised by
  a re-entrant `threading.RLock`. Writes happen on the loop thread; reads on
  request threads. WAL means readers never block the writer.
- **`MonitorState`** — an `RLock` guards the published snapshot
  (`publish_cycle`, `set_interface_info`) and the accessor methods, so an HTTP
  request never sees a half-updated cycle.
- **Detection state** (baseline deque, seen PIDs, cooldown map) — touched only by
  the loop thread, so it needs no lock. This is deliberate: locking detection
  would hide the fact that it is single-writer.
- **Notification queue** — `queue.Queue(maxsize=200)`. `handle_events` uses
  `put_nowait` and counts drops rather than applying back-pressure to the loop.

Signal handling: `SIGINT`/`SIGTERM` set `server.should_exit`; the loop is stopped
from the `finally` block, so Ctrl+C drains the notification queue and closes the
database cleanly.

---

## 5. Data model and storage

```
interface_measurements ──► time series, one row per interface per cycle
connections            ──► snapshot rows, written on change only
processes              ──► PID directory (upsert)
events                 ──► detections + JSON evidence
collector_health       ──► last known state per collector (survives restart)
```

Indexes exist for the read patterns that actually occur:
`timestamp DESC` (latest/history), `(interface_name, timestamp DESC)` (per-NIC
history), `(event_type, timestamp DESC)` (event filters and cooldown lookups),
`(remote_address, remote_port)` (connection search), `severity`, `pid`.

Storage volume, and why retention is layered:

| Table | Write rate | Window | Rationale |
|---|---|---|---|
| `interface_measurements` | 1 row/interface/cycle (~86 k rows/day for 1 NIC) | `retention_days` (7) | Small rows, the primary history |
| `connections` | only on change | `connection_retention_hours` (24) | Potentially hundreds of rows/cycle; an idle machine writes nothing |
| `events` | few per day | `retention_days` (7) | The explanation trail |
| `collector_health` | 1 upsert/cycle (best effort) | unbounded | Bounded by the number of collectors |

Retention runs at most once per `prune_interval_seconds` (default 1 h) and only
inside the loop; a failure to prune is logged at debug level and never stops
collection. See [ADR-0007](adr/0007-store-connection-snapshots-on-change.md).

**Schema evolution:** the schema is versioned with `PRAGMA user_version`
(currently 1). Migrations are expected to be additive `ALTER TABLE` statements
applied in `Database._create_schema` when the recorded version is lower;
destructive migrations are not planned because the data is regenerable
measurements, not user-authored records. See [PLAN.md](PLAN.md) for the v0.2
migration item.

---

## 6. Detection pipeline

```
RuleContext ──► [ rule.evaluate(ctx) for each enabled rule ] ──► candidates
                        │ (a rule that raises is logged and skipped)
                        ▼
                cooldown filter  (event_type, interface, pid, source)
                        │
                        ▼
                  events to store / notify / display
```

State that the engine owns, and why it is not in the rules:

| State | Purpose | Lifetime |
|---|---|---|
| `_connection_history` (deque, 120) | rolling baseline for `CONNECTION_SPIKE` | process |
| `_seen_pids` | "new" in `NEW_NETWORK_PROCESS` | process, seeded at startup |
| `_last_emitted` | cooldown so a sustained condition is one event, not 1/s | process |

Keeping rules pure means each can be tested with a hand-built context and no
fixtures; keeping the state in one place means its reset semantics are visible in
one file ([ADR-0003](adr/0003-deterministic-rules.md)).

**Severity is a property of the rule, not of the measurement.** A rule may raise
its severity in future without touching the collectors.

---

## 7. API and read models

The API is a read-mostly projection of `MonitorState`:

```
GET /api/status          headline health + SLIs (success ratio, durations)
GET /api/ready           depth probe: database, loop freshness, last-cycle errors
GET /api/metrics         Prometheus exposition
GET /api/interfaces      inventory + latest measurement per interface
GET /api/traffic         aggregate current rate
GET /api/traffic/history time series for the chart
GET /api/connections     latest snapshot, filterable in memory
GET /api/processes       live processes + fallback to the process directory
GET /api/events          paged, filterable, newest first
GET /api/events/{id}     one event with evidence
PATCH /api/events/{id}   acknowledge / resolve
```

Two deliberate properties:

1. **The request path prefers memory.** While the loop is running, reads come
   from `MonitorState`'s snapshot; only `--api-only` mode (or a restart before the
   first cycle) reads history back from SQLite. This is what makes an honest
   empty answer possible ([ADR-0006](adr/0006-live-snapshot-over-database.md)).
2. **Empty is a valid answer.** Every list endpoint returns `200` with an empty
   collection rather than `404`/`500`, so the dashboard can render a new install.

The dashboard is served by the same process from the packaged assets
(`src/network_monitor/web/`, resolved through `importlib.resources` with an
`LNM_WEB_DIR` override): `/` → `index.html`, `/static/...` → CSS/JS. Serving it
from the package rather than the repository root is what makes an installed wheel
a complete product, and the CI packaging job proves it by fetching the real page
from the installed wheel. Same-origin serving removes any need for CORS beyond
loopback development.

### Dashboard design principles

The front end is the only part of this project a non-programmer sees, so it is
held to the same rules as the collectors:

1. **It answers questions in order.** Right now → needs attention → trend → who →
   where → is the monitor healthy. Each section is a heading in the document
   outline, so the order is structure rather than a visual accident.
2. **It states its own freshness.** The masthead chip reads "live · updated 2 s
   ago", turns amber when a refresh lags and red when it fails, and says plainly
   when it is paused. Stale data presented as current is the worst failure mode a
   monitoring UI can have.
3. **Partial failure is visible, not fatal.** Endpoints are fetched in parallel
   with a timeout; sections that fail keep their last values and the banner names
   them. Nothing is blanked to hide a problem.
4. **Severity is never colour alone** — a word plus a glyph accompany every state,
   and the verdict region is announced through `aria-live`.
5. **Progressive disclosure**: headline → list → row → dialog → raw JSON. The
   connection table and collector detail sit behind `<details>`, so the page is
   readable at a glance without hiding anything permanently.
6. **Tokens, not ad-hoc values.** One token set drives light and dark, spacing,
   type and shape; a redesign becomes a change of values.
7. **No build step, no CDN, no fonts, no tracking.** Three static files, relative
   API paths (so the dashboard also works behind the preview proxy), and
   `textContent` only - never `innerHTML`.

These invariants are enforced by `tests/test_dashboard.py`; behaviour against real
API payloads is exercised in a DOM (jsdom) during development, because the Python
suite cannot run a browser.

---

## 8. Failure domains

| Domain | Failure | Blast radius | Detection | Documented response |
|---|---|---|---|---|
| PowerShell / `netstat` | blocked, slow, malformed | Connection features | `collector_up{collector="connections"} = 0`, `COLLECTOR_FAILURE` | [RUNBOOK §2](RUNBOOK.md) |
| psutil TCP table | `AccessDenied` (non-admin) | Process names only | `<access_denied>` in connections, degraded health | Re-run elevated |
| Process resolver | PID exited mid-cycle | One connection row shows `<no_such_process>` | `ProcessInfo.error` | Expected, no action |
| SQLite | locked / disk full | Storage writes for that cycle | `last_errors`, `failed_cycles_total`, `/api/ready` 503 | [RUNBOOK §3](RUNBOOK.md) |
| SQLite file | corrupt | Startup | Loud `sqlite3.DatabaseError` at start | [RUNBOOK §4](RUNBOOK.md) |
| Detection rule | exception | One rule | `logger.exception`, other rules unaffected | Fix the rule |
| SMTP | unreachable / auth | Notifications only | `lnm_notifications_dropped_total`, `delivered: false` in `/api/status` | [RUNBOOK §6](RUNBOOK.md) |
| Loop thread | unexpected exception | One cycle | `logger.exception("unexpected error in monitoring cycle")` | Inspect logs; loop self-heals |
| API process | crash | Everything | Nothing — process is gone | Restart (no state is lost) |

The design rule behind the table: **a failure must degrade a feature and be
visible in an existing metric or probe**. Silent degradation is the only
unacceptable outcome ([ADR-0004](adr/0004-failure-is-data.md)).

---

## 9. Capacity and limits

| Dimension | Design point | Where it breaks first |
|---|---|---|
| Interfaces | Dozens (labels are interface names) | Unbounded label cardinality if a host renames interfaces constantly (not expected) |
| Connections per cycle | ~10⁴ parsed and stored | PowerShell startup dominates beyond ~1 Hz polling |
| Events | Bounded by rules × subjects × cooldown | A flapping remote endpoint could produce event churn; the cooldown is the control |
| Retention | 7 days measurements / 24 h connections | Disk; the runbook covers growth checks |
| Metrics cardinality | 6 event types × 3 severities, plus interfaces and states | Deliberately no per-process labels |

Explicit non-goal: the monitor is not sized for line-rate or long-tail
retention. It is sized for "one person understanding one machine".

---

## 10. Extension points

| To add | Implement | Register in | Tests to add |
|---|---|---|---|
| A new metric source (Performance Counters, `GetExtendedTcpTable`) | A callable matching the existing provider signature (`ps_provider`, `tcp_table_reader`) or a new `Collector` | `ConnectionCollector.__init__` / `Monitor.__init__` | Parser tests + fallback ordering test |
| A new detection rule | Subclass `Rule`, return `Event`s | `default_rules()` | Rule unit tests + engine cooldown test |
| A new notifier (webhook, Windows toast) | Subclass `Notifier.send()` | `build_state()` notifier list | Policy tests + failure path |
| A new storage backend | The repository method surface | `build_state()` | Repository contract tests |
| A new API endpoint | `api/routes.py` | The router (auto-included) | `tests/test_api.py` |
| A new metric | `api/metrics.py` (bounded labels only) | `render_metrics` | Format + cardinality tests in `test_observability.py` |
| Schema change | `storage/schema.py` + version bump | `_create_schema` | Migration test on an old file |

Every extension point is injectable in tests, which is why the collectors take
providers as constructor arguments and the engine takes a rule list.

---

## 11. Security architecture

- **Surface:** loopback HTTP only by default; no authentication in the MVP — so
  the contract is "never bind off-host" (documented in README §13 and enforced by
  the default in `config.yaml`).
- **Input:** query parameters are typed and validated by FastAPI/Pydantic; event
  type and severity are checked against the enums and rejected with `422`.
- **Subprocesses:** fixed argument vectors, `shell=False`, `shutil.which` lookup,
  10-second timeout, no interpolation of user input. Two `# noqa: S603` sites are
  annotated as reviewed.
- **Data:** endpoints, ports, states, process names and executable paths only. No
  packet payloads, no credentials, no packet capture.
- **Secrets:** SMTP password via `LNM_SMTP_PASSWORD`; `.gitignore` excludes
  local config and generated data.

---

## 12. Observability architecture

| Signal | Artefact | Consumer |
|---|---|---|
| Structured logs | `logs/monitor.log` (rotating, text or JSON) | Human, log shipper |
| Metrics | `/api/metrics` (Prometheus text) | Scraper, Grafana, text checks |
| Health | `/api/health` (liveness), `/api/ready` (depth) | Supervisor, scripts |
| Status | `/api/status` (SLIs, collector detail, storage) | Dashboard, alerting scripts |
| Events | `events` table + `/api/events` | Human, email |

The SLOs derived from these signals — and the error-budget policy that decides
when to stop adding features and fix reliability — live in [SLO.md](SLO.md).

---

## 13. Decision log

| ADR | Decision |
|---|---|
| [0001](adr/0001-modular-monolith.md) | Modular monolith instead of services |
| [0002](adr/0002-sqlite-primary-store.md) | SQLite as the primary store |
| [0003](adr/0003-deterministic-rules.md) | Deterministic rules, no ML |
| [0004](adr/0004-failure-is-data.md) | A collector failure is data, not an exception |
| [0005](adr/0005-metering-vs-attribution.md) | Never claim per-process byte counts |
| [0006](adr/0006-live-snapshot-over-database.md) | Live snapshot preferred over database reads |
| [0007](adr/0007-store-connection-snapshots-on-change.md) | Store connection snapshots only on change |
| [0008](adr/0008-hand-rolled-prometheus-exposition.md) | Hand-rolled Prometheus exposition |
| [0009](adr/0009-json-logging-option.md) | Text and JSON logging layouts |
