# Plan

**Purpose:** what is being built next, in what order, and how the project is
managed while it happens. Scope boundaries come from
[SPECIFICATION.md §2.3](SPECIFICATION.md); operational targets come from
[SLO.md](SLO.md).

Ship-small cadence: one branch → one pull request → one release. Every item below
is sized so it can ship and be verified independently.

---

## 1. Where the project is

| Release | Theme | State |
|---|---|---|
| **v0.1.0** | Measurement and explanation MVP: collectors, SQLite, detection, API, dashboard, notifications | Shipped on `main` via PR #1 |
| **v0.1.x** | Operational hardening: structured logging, Prometheus metrics, depth readiness probe, CI/CD, runbook, SLOs | This branch |

Definition of "shipped": merged to `main`, CI green on Windows and Linux, tagged,
and documented in [CHANGELOG.md](../CHANGELOG.md).

---

## 2. Roadmap

### v0.2.0 — Operability at home (target: next 4–6 weeks)

The MVP answers "what is happening". v0.2 makes it answer "what happened last
night" without a human watching the log.

| ID | Item | Why | Acceptance criteria | Size |
|---|---|---|---|---|
| LNM-101 | Event-triggered digest: daily/weekly summary of events and traffic peaks (email, disabled by default) | The operator should not need to tail a log | Digest generated on schedule, rendered from stored data, non-blocking, covered by tests | M |
| LNM-102 | CSV export for measurements and events | The MVP serves JSON only; spreadsheets are how people actually inspect history | `GET /api/traffic/history?format=csv`, `GET /api/events?format=csv`, documented, tested | S |
| LNM-103 | Schema migration path (`PRAGMA user_version` upgrade routine) | v0.2 adds columns; the first migration must not require deleting data | Old-file fixture test: open v1 DB, migrate to v2, data intact | M |
| LNM-104 | Per-rule dry-run CLI (`--explain-detection`) | Tuning thresholds currently needs a live machine | Prints, for a given snapshot, why each rule fired or did not | S |
| LNM-105 | Windows Performance Counters collector (opt-in) | Cross-check psutil counters against the OS's own numbers | Enable via config; disagreement between sources is visible in `/api/status` | M |
| LNM-106 | Retention by size, not only by age | A busy machine can outrun a time-based policy | `database.max_size_mb`, enforced by the existing prune pass | S |

### v0.3.0 — Attribution depth

| ID | Item | Why | Acceptance criteria | Size |
|---|---|---|---|---|
| LNM-201 | `GetExtendedTcpTable()` collector (IP Helper API) | Removes the PowerShell cold-start from the hot path; single syscall source of truth | Same normalised model, selected ahead of PowerShell, parity tests against the JSON parser | M |
| LNM-202 | Process-tree attribution (`chrome.exe` → child renderer PIDs) | "Which app" is sometimes a tree, not a PID | Parent chain stored; API returns it; no per-process byte claims | M |
| LNM-203 | Connection lifecycle events (`NEW_REMOTE_ENDPOINT`, `PROCESS_STOPPED_USING_NETWORK`) | "When did this start?" is the most-asked question about an alert | Two new rules with cooldowns and evidence; suppressed by default on busy hosts | M |
| LNM-204 | Interface flap / state-change event | A NIC going down is a real abnormal condition the MVP cannot see | `INTERFACE_DOWN` event with before/after state | S |

### v0.4.0 — Usability

| ID | Item | Why | Size |
|---|---|---|---|
| LNM-301 | Dashboard time-range selector and per-interface chart filter | 15 minutes is not enough to answer "last night" | S |
| LNM-302 | Event acknowledge/resolve from the dashboard | The API already supports it; the UI should too | S |
| LNM-303 | Windows service / Task Scheduler packaging | "Runs all the time" beats "runs when I remember" | M |
| LNM-304 | Config hot-reload for detection thresholds | Changing a threshold should not drop collection for a restart | M |

### Explicitly not planned (unchanged from the specification)

Packet capture · deep packet inspection · ML anomaly detection · exact
per-process byte accounting · multi-machine aggregation · cloud deployment ·
authentication · PostgreSQL/Redis/Kafka · Kubernetes · microservices ·
containerisation.

Two of these deserve a note for future contributors:

- **Containerisation is not just unplanned, it is counter-indicated**: the
  monitor measures *the host it runs on*. A container sees a namespaced network
  stack, so containerising it would silently measure the wrong thing.
- **Authentication becomes a prerequisite the moment binding leaves loopback.**
  If a future version ever supports `--host 0.0.0.0`, auth and TLS ship in the
  same release or the feature does not ship.

---

## 3. Risk register

| # | Risk | Likelihood | Impact | Mitigation (already in place / planned) |
|---|---|---|---|---|
| R1 | PowerShell is blocked by policy or hooking (EDR) | Medium | Connection features degrade | Three-source fallback; failure is visible; [RUNBOOK §2](RUNBOOK.md#2-collector-failure--collector_failure-events) |
| R2 | Non-elevated run hides other users' socket owners | High on Linux, low on Windows | Attribution incomplete | Reported as `<access_denied>`; never guessed; documented |
| R3 | Unbounded log/DB growth on a busy host | Medium | Disk pressure | Rotating logs, layered retention, size-based retention (LNM-106) |
| R4 | Interface counter reset / adapter change | Medium | A nonsense rate spike | Reset policy in `RateCalculator`, unit-tested |
| R5 | Thresholds mis-tuned → event noise → alerts ignored | Medium | Operator stops reading events | Cooldowns, `min_baseline` guards, dry-run tuning (LNM-104) |
| R6 | Windows API changes (PowerShell cmdlet output shape) | Low | Parser breaks | Parser tests with recorded payloads; fallbacks; JSON schema is documented |
| R7 | Clock changes (sleep/resume, DST) | Medium | Freshness false alarms | Freshness uses timestamps, not monotonic time; documented restart recovery |
| R8 | Dependency compromise in the supply chain | Low | Repo-wide | Pinned ranges, Dependabot, `pip-audit` in CI, no shell execution, minimal dependencies (4 runtime) |
| R9 | A rule throws and silences detection | Low | Missed detections | Isolation per rule + fault-injection test |
| R10 | Silent degradation (the "untrustworthy tool" failure) | Medium | Operator trust lost | Readiness probe fails on errors; SLIs in `/api/status`; SLO-8 |

Review this register every release; add an entry for every production incident.

---

## 4. How the project is managed

### Labels

| Group | Labels | Meaning |
|---|---|---|
| Type | `type/bug`, `type/feature`, `type/docs`, `type/chore`, `type/security` | Kind of change |
| Area | `area/collectors`, `area/storage`, `area/detection`, `area/api`, `area/ui`, `area/ops`, `area/docs` | Ownership boundary (see [CODEOWNERS](../.github/CODEOWNERS)) |
| Priority | `priority/P0` (drop everything), `priority/P1` (this iteration), `priority/P2` (later) | Ordering |
| Status | `status/blocked`, `status/needs-info`, `status/good-first-issue` | Triage state |
| SRE | `sre/reliability`, `sre/observability`, `sre/security` | Cross-cutting work that follows the error-budget policy in [SLO.md](SLO.md) |

### Intended triage, per roadmap item

The tables in §2 state the work; this one states how each item is filed, so a
fresh clone can be re-triaged identically without guessing.

| Item | Labels | Milestone |
|---|---|---|
| LNM-101 | `type/feature`, `area/ops`, `priority/P1`, `sre/observability` | v0.2.0 |
| LNM-102 | `type/feature`, `area/api`, `priority/P2`, `status/good-first-issue` | v0.2.0 |
| LNM-103 | `type/feature`, `area/storage`, `priority/P1`, `sre/reliability` | v0.2.0 |
| LNM-104 | `type/feature`, `area/detection`, `area/ops`, `priority/P2`, `status/good-first-issue` | v0.2.0 |
| LNM-105 | `type/feature`, `area/collectors`, `priority/P2`, `sre/observability` | v0.2.0 |
| LNM-106 | `type/feature`, `area/storage`, `priority/P2`, `sre/reliability` | v0.2.0 |
| LNM-201 | `type/feature`, `area/collectors`, `priority/P1` | v0.3.0 |
| LNM-202 | `type/feature`, `area/collectors`, `area/api`, `priority/P2` | v0.3.0 |
| LNM-203 | `type/feature`, `area/detection`, `priority/P2` | v0.3.0 |
| LNM-204 | `type/feature`, `area/detection`, `priority/P2` | v0.3.0 |
| LNM-301 | `type/feature`, `area/ui`, `priority/P2` | v0.4.0 |
| LNM-302 | `type/feature`, `area/ui`, `priority/P2` | v0.4.0 |
| LNM-303 | `type/feature`, `area/ops`, `priority/P2`, `sre/reliability` | v0.4.0 |
| LNM-304 | `type/feature`, `area/detection`, `area/ops`, `priority/P2`, `sre/reliability` | v0.4.0 |

`priority/P0` and `status/blocked` / `status/needs-info` are applied during
triage, never in advance: an issue that has not been read yet is not P0.

### Milestones

One milestone per release (`v0.2.0`, `v0.3.0`, …). A milestone closes only when
every issue in it is closed or explicitly moved — no silent debt.

### Issue workflow

```
triage (labels + milestone) → ready (acceptance criteria written) → in progress
   → PR (CI green, docs updated) → review → merge → milestone →
   release (tag + CHANGELOG) → verify against the acceptance criteria → close
```

**Definition of Ready** (an issue may be started when): the problem is stated,
acceptance criteria are testable, area label is set, and any SLO impact is noted.

**Definition of Done** (the same as [CONTRIBUTING.md §6](../CONTRIBUTING.md)):
tests, lint, docs, changelog entry, and — for anything operational — an entry in
the runbook or SLO document.

### Cadence

| Ritual | Frequency | Output |
|---|---|---|
| Dependency/security review | Weekly | Dependabot PRs merged or dismissed with a reason |
| SLO review | Weekly | `SLO.md §6` updated when the budget is touched |
| Roadmap review | Monthly | Items promoted, dropped, or re-sized here |
| Release | On demand, when a milestone is complete | Tag + GitHub release with artifacts |

---

## 5. Dependency policy

Four runtime dependencies (`fastapi`, `uvicorn`, `psutil`, `PyYAML`), chosen
because each replaces a large amount of code the project would otherwise own.
Rules:

1. A new runtime dependency needs a justification in the PR: what it replaces,
   why the standard library cannot do it, and its maintenance signal. The
   metrics endpoint is the precedent for *declining* one
   ([ADR-0008](adr/0008-hand-rolled-prometheus-exposition.md)).
2. Ranges are compatible-release (`>=X.Y`), never unpinned majors.
3. Application code imports no dependency to do arithmetic, parsing or text
   formatting that the standard library already does well.
4. Dev dependencies are unlimited in principle but also reviewed; CI must pass
   with only `requirements-dev.txt` installed.
