# Service Level Objectives

SLOs for a single-machine tool can look like theatre. They are not, if they are
written for the person who actually depends on the monitor: the operator who
needs to trust that what the dashboard says is what the machine did. This
document defines what "trustworthy" means numerically, how each number is
measured from data the product already exposes, and what happens when the budget
is spent.

Everything here is measurable **today**, with no additional tooling:

```powershell
# The SLIs, straight from the running monitor
curl -s http://127.0.0.1:8000/api/status    # success ratio, durations, freshness
curl -s http://127.0.0.1:8000/api/ready     # depth probe
curl -s http://127.0.0.1:8000/api/metrics   # Prometheus series
```

---

## 1. Service level indicators

| SLI | Definition | Source | Type |
|---|---|---|---|
| **Collection success ratio** | cycles without a collector/storage error ÷ completed cycles | `/api/status.collection_success_ratio`, `lnm_failed_cycles_total / lnm_cycles_total` | ratio over window |
| **Cycle latency** | duration of one collection cycle | `lnm_cycle_duration_seconds`, `..._max` | distribution (latest + max) |
| **Data freshness** | age of the served snapshot | `now − lnm_last_cycle_timestamp_seconds` | gauge |
| **Collector availability** | per collector, last attempt succeeded | `lnm_collector_up{collector=...}` | gauge |
| **API availability** | the dashboard/API answered a request | `/api/health` (liveness), `/api/ready` (depth) | probe success ratio |
| **Storage headroom** | database size on disk | `lnm_storage_bytes` | gauge |
| **Notification success** | delivered ÷ attempted notifications | `lnm_notifications_dispatched_total`, `/api/status.notifications.recent[].delivered` | ratio |
| **False-positive pressure** | events per day by type | `lnm_events_24h{severity}`, `lnm_events_total{...}` | counter |

---

## 2. Objectives

Targets are per rolling window. Given the deployment (one user, one machine,
1 Hz), the windows are short and the targets tight where the cost of failure is
"the operator stops trusting the tool".

| Objective | Target | Window | Rationale |
|---|---|---|---|
| SLO-1 Collection success ratio | **≥ 99.5 %** | 7 days | A missed minute per day is tolerable; more means the data lies |
| SLO-2 Cycle latency p95 | **≤ 250 ms** | 7 days | Must stay well inside the 1 s interval so cycles do not queue |
| SLO-3 Data freshness (fresh while running) | **≤ 3 × interval** | continuous | `/api/ready` fails beyond this, so the number and the probe agree |
| SLO-4 Interface metering availability | **≥ 99.9 %** | 7 days | Metering is the core promise; it has no external dependency but psutil |
| SLO-5 Connection attribution availability | **≥ 95 %** | 7 days | Depends on PowerShell/policy/elevation — a genuinely weaker dependency |
| SLO-6 Event capture (no rule errors) | **100 %** of cycles with a rule exception = 0 | 7 days | A crashing rule is a defect, not a degradation |
| SLO-7 Notification delivery (when enabled) | **≥ 99 %** of queued events | 7 days | Below this, the operator stops reading the mail |
| SLO-8 Readiness truthfulness | `/api/ready` = 200 **only if** the last cycle was clean and fresh | always | A probe that lies is worse than no probe |

**Non-objectives (deliberately not promised):** packet-level completeness,
sub-second resolution, exact per-process volume (see
[ADR-0005](adr/0005-metering-vs-attribution.md)), and availability while the host
is asleep.

---

## 3. Error budget

For SLO-1 (99.5 % over 7 days ≈ 10 080 minutes): the budget is ≈ **50 minutes of
degraded/failed cycles per week**.

Burn policy — actions are triggered by the budget, not by mood:

| Budget consumed | Action |
|---|---|
| < 50 % | Normal feature work |
| ≥ 50 % | Reliability work takes priority over new features in the next iteration; add a regression test for the dominant failure mode |
| ≥ 100 % (exhausted) | Feature freeze on the affected component; the fix is the only work item until two consecutive clean days; post-incident note added to this file's §6 |

For SLO-2, treat "cycle latency above the interval for 10 consecutive cycles" as
budget-consuming regardless of the ratio: it means the meter is falling behind
real time.

---

## 4. Measuring and alerting without extra infrastructure

No Prometheus is required. Two local mechanisms already exist:

1. **Detection rules as alerts.** `COLLECTOR_FAILURE` (critical) is the
   in-product alert for SLO-4/SLO-5 breaches. Sustained high bandwidth
   (`HIGH_DOWNLOAD`/`HIGH_UPLOAD`) and `CONNECTION_SPIKE` cover the network-side
   conditions the operator cares about.
2. **A scrape, if the operator wants dashboards.** Any Prometheus-compatible
   agent can scrape `/api/metrics`; the series are named for the SLIs above.

Suggested alert expressions (for a Grafana/Prometheus stack, matching the
metrics that exist in this repository):

| Alert | Expression (conceptual) | Severity |
|---|---|---|
| MeteringDegraded | `lnm_collector_up{collector="interface"} == 0` for 3 cycles | critical |
| CollectionFailing | `increase(lnm_failed_cycles_total[5m]) > 5` | warning |
| CycleSlow | `lnm_cycle_duration_seconds > 0.5` for 5 cycles | warning |
| StaleData | `time() - lnm_last_cycle_timestamp_seconds > 15` | critical |
| EventStorm | `increase(lnm_events_total[1h]) > 60` | warning |
| DiskGrowth | `deriv(lnm_storage_bytes[6h]) > 50e6` | warning |

Alerting philosophy: **page for staleness and metering loss, ticket for
everything else.** A local tool that pages on a single noisy event is a tool the
operator turns off.

---

## 5. Verification cadence

| Cadence | Check | Command / artefact |
|---|---|---|
| Every commit | Unit + integration + API + fault-injection tests | `python -m pytest` (269 tests) |
| Every commit | Lint, format, security lint | `ruff check`, `ruff format --check` |
| Every pull request | CI on Linux + Windows, Python 3.11 + 3.12 | `.github/workflows/ci.yml` |
| Weekly (dev) | Dependency audit, coverage trend | `pip-audit`, coverage report in CI |
| Weekly (operator) | SLO review from `/api/status`, `/api/metrics` | This document + [RUNBOOK.md](RUNBOOK.md) |
| Monthly | Restore drill: copy `data/monitor.db`, delete, restart, verify regeneration | [RUNBOOK §4](RUNBOOK.md#4-database-corruption-or-loss) |
| Per release | Chaos pass: run the fault-injection suite explicitly | `pytest tests/test_fault_injection.py` |

---

## 6. Notes and incident log

Record here any budget-exhausting event: date, symptom, detection, root cause,
fix, and the test that now prevents it.

| Date | SLO | Symptom | Root cause | Fix | Regression test |
|---|---|---|---|---|---|
| 2026-10-04 | SLO-1 | `COLLECTOR_FAILURE: system` every 15 s while healthy | the system collector ran only on the first cycle, so `last_success` went stale | run it every cycle | `test_monitor.py::TestCollectorHealthCadence::test_system_collector_runs_every_cycle` |
| 2026-10-04 | — | Connection table growing without bound | snapshots written every second even when unchanged | write only on change + 24 h connection retention | `test_monitor.py::TestConnectionSnapshotStorage` |
| 2026-10-04 | SLO-8 | `lnm_events_24h` exported a nonsense value | malformed expression in the metrics builder | rewritten with an explicit loop over severities | `test_observability.py::TestMetricsEndpoint` |
| 2026-10-04 | — | CI matrix red on all four jobs before any test ran | `pytest-cov` was missing from `requirements-dev.txt`; the local venv had it installed by hand | added it plus the other developer tools to `requirements-dev.txt` and `[project.optional-dependencies].dev` | CI itself: `--cov` is now resolvable in a clean environment |
| 2026-10-04 | SLO-2 | Windows jobs failed after the Linux jobs turned green | a process-resolution test spawned the POSIX-only `sleep` binary, and a failed start against a corrupt database kept the SQLite handle open, blocking the runbook's recovery step on Windows | `sys.executable -c "import time; time.sleep(...)"`; `Database.connect()`/`build_state()` release the handle on failure | `test_collectors.py::TestProcessResolver::test_process_that_exits_between_steps`, `test_fault_injection.py::TestStorageFailures::test_corrupt_database_fails_loudly_and_recovers` |

Both product bugs above were found by running the monitor, not by reading it —
which is itself the argument for the readiness probe and the SLI table in §1.
