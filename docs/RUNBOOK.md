# Operational runbook

**Scope:** the operator of a Local Network Monitor instance on one Windows host
(Linux/macOS noted where behaviour differs).
**Jurisdiction:** symptoms, diagnosis, mitigation, verification, recovery.
**Not in scope:** design rationale ([ARCHITECTURE.md](ARCHITECTURE.md)), targets
([SLO.md](SLO.md)), roadmap ([PLAN.md](PLAN.md)).

The monitor is a passive observer: no mitigation below ever changes network
state. The worst outcome of a mistimed action is losing history, so every
procedure starts with "preserve the data".

---

## 0. First response checklist

When something looks wrong, in this order:

```powershell
# 1. Is the process alive, and is the loop fresh?
curl -s http://127.0.0.1:8000/api/health     # {"status":"ok"}  -> process alive
curl -s http://127.0.0.1:8000/api/ready      # 200 / 503 + per-check booleans

# 2. What does the product think is wrong?
curl -s http://127.0.0.1:8000/api/status     # status, collectors, last_errors, SLIs
curl -s "http://127.0.0.1:8000/api/events?limit=20"

# 3. What does the machine think happened?
Get-Content logs\monitor.log -Tail 80
```

Decision tree:

```
/api/ready 503
├── checks.database = false ......... §3 storage
├── checks.collection_loop = false .. §1 stale loop / §2 collector failure
└── checks.collection_errors = false . §2, then read status.last_errors

/api/ready 200 but dashboard looks wrong
├── no data at all .................. §7 dashboard
├── missing process names ........... §5 attribution
└── no emails ....................... §6 notifications
```

---

## 1. Stale data — `/api/ready` reports `collection_loop = false`

**Symptom.** Dashboard timestamp stops advancing; `lnm_last_cycle_timestamp_seconds`
grows; the probe fails after `3 × collection_interval`.

**Diagnose**

```powershell
curl -s http://127.0.0.1:8000/api/status | Select-String -Pattern "last_cycle|cycles|failed"
Get-Content logs\monitor.log -Tail 50 | Select-String "unexpected error|Traceback"
```

**Mitigate**

1. If the log shows `unexpected error in monitoring cycle` repeatedly, capture the
   traceback, then restart the process (`Ctrl+C`, re-run). The loop is
   self-healing per cycle, so a repeating traceback is a defect worth reporting.
2. If the log is silent and `cycles` is still incrementing, the *clock* moved
   (sleep/resume, timezone change) — the freshness check compares wall-clock
   timestamps; a short restart re-baselines it.
3. If the process is gone, restart. Nothing is lost: the database is the record.

**Verify.** `cycles` increases over ~5 s and `/api/ready` returns 200.

---

## 2. Collector failure — `COLLECTOR_FAILURE` events

**Symptom.** `lnm_collector_up{collector="connections"} = 0`, events of type
`COLLECTOR_FAILURE`, `/api/status.collector_states.connections` is `degraded`
(1 failure) or `failed` (2+).

Interface metering should still be healthy; if it is not, treat it as §1.

**Diagnose per collector**

| Collector | Check |
|---|---|
| `connections` | Run the source by hand: `Get-NetTCPConnection \| Measure-Object`. Then check policy/permissions and whether `powershell.exe` is on `PATH`. |
| `interface` | `python -c "import psutil; print(psutil.net_io_counters(pernic=True))"` |
| `processes` | Same, for `psutil.net_connections(kind='tcp')`; `AccessDenied` is expected for other users' sockets without elevation |
| `system` | `python -c "import psutil; print(psutil.cpu_percent(), psutil.pids()[:3])"` |

**Mitigate**

- **PowerShell blocked or slow** — the monitor already falls back to `psutil`,
  then `netstat -ano`. Check `/api/status.collectors.connections.last_error` to
  see which source failed and why. If the command is slow (hundreds of
  connections, cold start), raise `monitor.collection_interval` to 2–5 s.
- **`AccessDenied`** — run the monitor elevated if process attribution matters;
  otherwise accept `<access_denied>` rows, which are reported honestly.
- **Persistent, with a foreign cause** (AV interfering, EDR hooking PowerShell) —
  disable the affected feature path rather than the monitor: there is no config
  switch for individual collectors, so set `collection_interval` higher and treat
  `COLLECTOR_FAILURE` as accepted noise, or run `--api-only` if collection is
  impossible.

**Verify.** `lnm_collector_consecutive_failures` returns to 0 and a
`NEW_NETWORK_PROCESS`/`HIGH_*` event can still be produced (trigger traffic).

---

## 3. Storage failures — `checks.database = false`, `disk is full`, `database is locked`

**Symptom.** `/api/status.last_errors` contains `storage: ...`; `failed_cycles`
climbs; `/api/ready` 503.

**Diagnose**

```powershell
Get-PSDrive C                                   # free space
curl -s http://127.0.0.1:8000/api/status        # storage row counts + path
Get-ChildItem data\monitor.db*                  # db, -wal, -shm
```

**Mitigate**

| Cause | Action |
|---|---|
| Disk full | Free space, then confirm writes resume. Lower `database.retention_days` and/or `connection_retention_hours` for immediate effect. |
| `database is locked` transiently | Another process (a backup tool, an antivirus scanner, DB Browser) holds the file. Exclude `data/` from real-time scanning; the monitor retries next cycle. |
| Persistent lock | Stop the monitor, confirm no other process has the file open, restart. WAL means a killed process does not corrupt the database. |
| WAL file growing | Normal under write load; a `VACUUM`/checkpoint happens on clean shutdown. A multi-GB `-wal` file suggests a long-running reader — restart to checkpoint. |

**Verify.** One clean cycle: `/api/status.last_errors` becomes `[]` and
`lnm_failed_cycles_total` stops increasing.

---

## 4. Database corruption or loss

**Symptom.** Startup raises `sqlite3.DatabaseError: file is not a database`, or
the file is missing/zero-length.

This is the one failure that stops the process, deliberately: the monitor refuses
to run against data it cannot trust.

**Preserve, then recover**

```powershell
# 1. Preserve everything, always, before touching anything
Copy-Item data\monitor.db   "data\monitor.corrupt-$(Get-Date -Format yyyyMMdd-HHmmss).db"
Copy-Item data\monitor.db-wal "data\monitor.corrupt-$(Get-Date -Format yyyyMMdd-HHmmss).db-wal" -ErrorAction SilentlyContinue

# 2. Try a recovery dump (salvages most rows, including from a partial file)
sqlite3 data\monitor.db ".recover" | sqlite3 data\recovered.db

# 3. If recovery works, swap it in; if not, start fresh (schema is recreated)
Move-Item data\recovered.db data\monitor.db
Remove-Item data\monitor.db-wal, data\monitor.db-shm -ErrorAction SilentlyContinue

# 4. Start and confirm
python -m network_monitor --check-config
python -m network_monitor --once
```

**Recovery objectives.** RTO: seconds (restart). RPO: at most one collection
interval of measurements; events are the only non-regenerable rows, and they are
also in `logs/monitor.log` (JSON layout), so keep the log if events matter.

**Preventive backup.** The database is a single file; a scheduled copy while the
monitor runs is safe in WAL mode:

```powershell
Copy-Item data\monitor.db "D:\backup\monitor-$(Get-Date -Format yyyyMMdd).db"
```

Weekly restore drill is defined in [SLO.md §5](SLO.md#5-verification-cadence).

---

## 5. Missing or wrong process attribution

**Symptom.** Connection rows show `unknown`, `<access_denied>`, or a PID with no
name.

**Expectation setting.** On Windows, ownership comes from
`Get-NetTCPConnection -OwningProcess`, so names are normally exact. On Linux/macOS
without `CAP_NET_ADMIN`, the kernel hides owners for other users' sockets, and
the monitor reports that fact rather than inventing a name.

**Diagnose**

```powershell
curl -s "http://127.0.0.1:8000/api/connections?process=unknown&limit=5"
curl -s http://127.0.0.1:8000/api/status | Select-String "connections"
```

**Mitigate.** Elevate the process (Windows: "Run as administrator"; Linux:
`setcap cap_net_admin+ep`) if names matter. Exclude the monitor's own Python
process noise by filtering in the UI. Do **not** expect per-process byte
counts — that is out of scope by design ([ADR-0005](adr/0005-metering-vs-attribution.md)).

---

## 6. Notifications not arriving

**Symptom.** Email enabled, events are being detected, no mail.

**Diagnose**

```powershell
curl -s http://127.0.0.1:8000/api/status | Select-String -Pattern "notifications" -Context 0,20
```

Look at `notifications.dropped`, `suppressed`, `worker_running`, and
`notifications.recent[].delivered` / `.detail`. Then:

| Observation | Meaning | Action |
|---|---|---|
| `worker_running: false` with `enabled: true` | no notifier configured or start failed | check `notifications.email.enabled/smtp_host/sender/recipients` |
| `suppressed` climbing | cooldown working as designed | lower `notifications.cooldown_seconds` only if the operator wants more mail |
| `dropped` climbing | queue full (SMTP hanging) | fix SMTP reachability; the loop is unaffected by design |
| `delivered: false`, detail mentions auth | credentials | set `LNM_SMTP_PASSWORD`; TLS/port sanity |
| No attempts at all | severity below floor | `notifications.min_severity` is at or below the event severity |

**Verify.** Trigger a high-severity event (large download) and check
`recent[].delivered: true`.

---

## 7. Dashboard reachable but showing nothing

| Cause | Check | Fix |
|---|---|---|
| First cycle not finished | `/api/status.cycles` is 0 | wait one interval |
| `--api-only` against a fresh database | `cycles` stays 0 | run without `--api-only` |
| Everything excluded | `monitor.exclude_interfaces` | remove the interface from the list |
| Browser caching a stale page | hard reload | `Ctrl+F5` |
| Rate is 0 while bytes climb | first cycle only sets a baseline | wait one cycle |

---

## 8. Routine operations

| Task | Procedure |
|---|---|
| Start | `python -m network_monitor` (or `.\scripts\run.ps1`) |
| Verify before trusting | `python -m network_monitor --once` |
| Validate a config change | `python -m network_monitor --check-config` — exit code 2 on error |
| Stop | `Ctrl+C` (drains the notification queue, closes the DB) |
| Rotate logs | automatic (`logging.max_bytes`, `backup_count`); delete old files freely |
| Trim history | lower `retention_days` / `connection_retention_hours`, or delete the DB and restart |
| Change thresholds | edit `config.yaml`, restart, confirm with `--check-config` |
| Inspect raw history | `sqlite3 data/monitor.db "select * from events order by timestamp desc limit 20"` |

---

## 9. Escalation and evidence to collect

When reporting a problem (or opening an issue), collect:

1. Version and platform: `/api/status.version`, `platform` from `/api/system`
2. `logs/monitor.log` covering the window (JSON layout preferred for machine reading)
3. `/api/status` output — collector states, `last_errors`, SLIs
4. `/api/events` around the time in question
5. Cycle duration and freshness (`last_cycle_duration_ms`, `last_cycle_at`)
6. What changed (config edit, Windows update, AV policy, new VPN/virtual adapter)

Redact remote addresses and process names if the report leaves the machine.

---

## 10. Change management

The monitor is operated locally, so the discipline is lightweight but explicit:

| Change | Required |
|---|---|
| Configuration | `--check-config` before restart; record the previous file |
| Code | PR with tests; CI green on Windows and Linux; [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Schema | Additive migration + old-file test; never destructive |
| Thresholds | Change one at a time, observe for a week, note it in [SLO.md §6](SLO.md#6-notes-and-incident-log) if it moves event volume |
| Rollback | `git checkout <previous tag>` + restart; the database stays compatible for additive migrations |
