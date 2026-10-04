# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Public API = the REST endpoints and the configuration file.

## [Unreleased]

### Added

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
- **Tests**: `test_config.py`, `test_observability.py`, `test_cli.py`,
  `test_notifications.py`, `test_fault_injection.py` (247 tests, 88 % coverage;
  CI fails below 85 %).

### Changed

- `/api/status` reports `degraded` when the last cycle recorded errors, not only
  when a collector is in the `failed` state.
- Readiness fails when the most recent cycle was not clean, so the probe cannot
  report "ready" while serving data assembled from failed collectors.
- The whole package is `ruff format`-clean under the repository's 100-column
  configuration, and `ruff check` (E4/E7/E9/F/I/B) reports no findings.

### Fixed

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

## [0.1.0] - 2026-10-04

Initial MVP: a local, single-machine network monitor that measures, explains and
displays host network activity.

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

[Unreleased]: https://github.com/deathtoconding/local-network-monitor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deathtoconding/local-network-monitor/releases/tag/v0.1.0
