# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Public API = the REST endpoints and the configuration file.

## [Unreleased]

Nothing yet. Planned work is tracked in [docs/PLAN.md](docs/PLAN.md)
(roadmap items LNM-101 through LNM-304) and filed as issues on the repository.

## [0.1.0] - 2026-10-04

The first public release: a local, single-machine network monitor that measures,
explains and displays what this machine is doing on the network, together with
the operability work - metrics, readiness, structured logging, CI/CD and the
dashboard design system - that makes it safe to run unattended.

### Added

- **Interface metering** (`psutil.net_io_counters`) with reset-safe
  bytes-per-second rates: a decreasing counter discards the negative delta,
  reports `0 B/s` and re-establishes the baseline.
- **TCP connection collection** with owning PIDs, from `Get-NetTCPConnection`
  (primary on Windows) with `psutil` and `netstat -ano` fallbacks, all normalised
  to one model.
- **Process attribution**: PID → name, executable, creation time, status, with a
  short-lived cache and an explicit error taxonomy (`no_such_process`,
  `access_denied`, `zombie`, `error`).
- **SQLite storage** (WAL, auto-created schema, `PRAGMA user_version`) with
  repositories for measurements, connections, processes, events and collector
  health; history survives restarts.
- **Six deterministic detection rules**: `HIGH_DOWNLOAD`, `HIGH_UPLOAD`,
  `INTERFACE_ERROR` (counter deltas), `NEW_NETWORK_PROCESS`,
  `CONNECTION_SPIKE` (rolling baseline), `COLLECTOR_FAILURE`, with per-subject
  cooldowns and structured evidence.
- **REST API** (`/api/status`, `/api/interfaces`, `/api/traffic`,
  `/api/traffic/history`, `/api/connections`, `/api/processes`, `/api/events`,
  `/api/events/{id}`, plus `/api/system` and `PATCH /api/events/{id}`) with
  OpenAPI docs at `/api/docs`.
- **Web dashboard** (no build step, no CDN): overview, traffic history chart,
  interfaces table, processes & connections with filters, events feed with
  click-through evidence.
- **Notifications**: policy/transport split with a bounded queue and a worker
  thread, so SMTP delivery can never block collection.
- **Resilience**: every collector is isolated; failures are recorded per
  collector and surfaced as `COLLECTOR_FAILURE` events, and the loop keeps
  running.
- **Retention**: age-based pruning for measurements and events, a shorter window
  for connection snapshots, and size/row counters in `/api/status`.
- **CLI**: `python -m network_monitor` with `--config`, `--host`, `--port`,
  `--interval`, `--api-only`, `--once`, `--check-config`, `--log-level`,
  `--version`; a Windows quick-start script at `scripts/run.ps1`.

- **Observability**: `GET /api/metrics` (Prometheus text exposition, no client
  library), `GET /api/ready` (depth probe: database writable, loop fresh, last
  cycle clean), and SLI fields in `GET /api/status`
  (`failed_cycles`, `collection_success_ratio`, `max_cycle_duration_ms`, `ready`).
- **Structured logging**: `logging.format: text | json`. The JSON layout emits
  one object per line with stable keys plus contextual fields passed via
  `extra=`, for log shippers and alerting.
- **Documentation**: [ARCHITECTURE.md](docs/ARCHITECTURE.md),
  [SLO.md](docs/SLO.md), [RUNBOOK.md](docs/RUNBOOK.md), [PLAN.md](docs/PLAN.md),
  nine [ADRs](docs/adr/README.md), [CONTRIBUTING.md](CONTRIBUTING.md),
  [SECURITY.md](SECURITY.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- **CI/CD**: lint + format + test matrix (Windows/Linux, Python 3.11/3.12) with an
  85 % coverage gate, package build and `twine check`, `pip-audit`,
  CodeQL, Dependabot, and a tag-driven release workflow that attaches artifacts.
- **Developer tooling**: `Makefile`, `scripts/dev.ps1`, `scripts/dev.sh`,
  `.pre-commit-config.yaml`, issue/PR templates, `CODEOWNERS`.
- **Tests**: a suite that runs unchanged on Windows and Linux; the only
  platform-specific behaviour is injected through `platform_name=` rather
  than monkeypatching `sys.platform`.
- **Tests**: `test_config.py`, `test_observability.py`, `test_cli.py`,
  `test_notifications.py`, `test_fault_injection.py` (247 tests, 88 % coverage;
  CI fails below 85 %).

### Changed

- **Dashboard redesigned as a story** instead of a grid of tables. It now answers,
  in order: *right now* (plain-language verdict, live rates, 15-minute sparkline,
  readiness), *needs attention* (warnings and failures with their recorded
  reasoning, informational events folded away), *traffic trend* (area chart with
  hover crosshair and a caption stating samples/window/peak), *who* (processes
  ranked by open connections, then the full connection table behind a disclosure),
  *where* (per-interface detail with error highlighting) and *the monitor itself*
  (collector runs/failures, readiness checks, storage, host facts).
- The dashboard states its own freshness (live / lagging / stale / paused) and
  names the sections that failed to refresh instead of blanking them; a first
  light/dark theme with a remembered toggle, semantic design tokens, skeletons,
  honest empty states, and an evidence dialog that labels rule evidence in words
  rather than showing raw keys.
- Accessibility is now part of the tests: one `h1`, landmarks, a skip link, an
  `aria-live` verdict, captions and scoped headers on every table, a glyph and a
  word alongside every severity, visible focus, and `prefers-reduced-motion` /
  `prefers-color-scheme` support.
- `tests/test_dashboard.py` (18 tests) turns the dashboard's structural promises
  into assertions: every element the script queries must exist, no remote
  resource may be referenced, API calls stay relative, `innerHTML` is forbidden,
  and the token set, state hooks and story order must stay defined.

- `/api/status` reports `degraded` when the last cycle recorded errors, not only
  when a collector is in the `failed` state.
- Readiness fails when the most recent cycle was not clean, so the probe cannot
  report "ready" while serving data assembled from failed collectors.
- The whole package is `ruff format`-clean under the repository's 100-column
  configuration, and `ruff check` (E4/E7/E9/F/I/B) reports no findings.

### Fixed

- The source distribution shipped the code and the tests but not the
  documentation: no `docs/`, no `config.yaml`, no `CONTRIBUTING.md`/`SECURITY.md`.
  `MANIFEST.in` now carries the full documentation set, the sample configuration
  and the developer tooling, and the CI packaging job asserts that they are
  present, so a reader who downloads the sdist can build, run and understand the
  project from one file.
- The dashboard markup was served with no `Cache-Control` header, so a browser
  could replay a stale copy after a redesign - a live monitoring page is exactly
  the wrong thing to serve from cache. `/` now answers `no-store,
  must-revalidate`; the versioned static assets are unaffected.
- `ProcessResolver` treated a cache entry as valid when its age equalled the
  TTL. Windows' coarse monotonic clock made that observable: a processor
  cache configured with `cache_ttl_seconds = 0` still served entries.
- A failed start against a corrupt database left its SQLite connection open,
  which on Windows blocked the documented recovery step of moving the file
  aside. `Database.connect()` and `build_state()` now release what they
  opened before re-raising, so the failure stays loud without holding a lock.
- The system collector ran only on the first cycle, so its `last_success` went
  stale and produced `COLLECTOR_FAILURE` events every 15 s on a healthy monitor.
- Connection snapshots were written every cycle even when unchanged; they are now
  stored only on change and pruned on a 24-hour window.
- `lnm_events_24h` in the metrics exposition computed a nonsense value.
- `ProcessInfo.from_row` crashed on rows without the derived `connection_count`
  column (affected `/api/processes` in the fallback path).
- The dashboard assets lived outside the package, so an installed wheel served the
  API with no dashboard. They now ship inside `network_monitor/web/` and the CI
  packaging job fetches the real page from the installed wheel.

[Unreleased]: https://github.com/deathtoconding/local-network-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deathtoconding/local-network-monitor/releases/tag/v0.1.0
