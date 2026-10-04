# Security policy

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting: *Security → Report a vulnerability* on
<https://github.com/deathtoconding/local-network-monitor/security/advisories/new>.

Include: affected version/commit, platform, a minimal reproduction, impact, and
whether you are willing to be credited. You will get an acknowledgement within
**72 hours** and an assessment (severity, fix plan, timeline) within **7 days**.
Coordinated disclosure after a fix ships; credit given unless you prefer
otherwise.

---

## What this software is (and is not)

Local Network Monitor is a **local, passive, single-user observation tool**. It
reads network counters, the TCP table and the process table, stores them in a
SQLite file, and serves a dashboard on loopback. It does not:

- capture or inspect packet payloads
- change firewall, routing or adapter state
- execute commands derived from user or network input
- transmit anything off-host unless email notifications are explicitly configured

Threat model in one line: *protect the integrity and confidentiality of the
monitoring data on the host it runs on, and never become an execution surface.*

---

## Trust boundaries

| Boundary | Trusted | Untrusted |
|---|---|---|
| HTTP API | `127.0.0.1` clients (no auth by design) | anything off-host — see below |
| Subprocesses | fixed argv we construct (`powershell`, `netstat`) | their **output**, which is parsed defensively |
| SQLite file | the local filesystem | the file's contents if tampered with |
| SMTP | configured server | its error messages, which are logged, never executed |
| Configuration | the operator editing `config.yaml` | — (but values are validated and rejected when out of range) |

### The one deployment rule

**Never bind the API to a non-loopback interface while authentication is absent.**
`monitor.host` defaults to `127.0.0.1`; the dashboard has no auth, no CSRF token
and no rate limiting because it assumes a single local user. If you use
`--host 0.0.0.0`, you are exposing: interface names and rates, every TCP endpoint
and its owning process name, and the executable paths of network-active
processes, to whoever can reach that port. CORS is restricted to loopback
origins, but CORS is a browser control, not access control.

The project's own position (documented in [PLAN.md §2](docs/PLAN.md)) is that if
remote access is ever supported, authentication and TLS ship in the same release
or the feature does not ship.

---

## Defensive design already in place

- **Subprocess safety** — fixed argument vectors, `shell=False`, absolute lookup
  via `shutil.which`, 10-second timeouts, no interpolation of API/config data into
  commands.
- **Input validation** — typed query parameters; enumerated values validated
  against `EventType`/`EventSeverity`/`EventStatus` and rejected with `422`;
  timestamps parsed strictly.
- **Output encoding** — the dashboard escapes all interpolated values before
  insertion into the DOM (see `web/js/app.js::escapeHtml`), so a process name
  containing markup cannot become script.
- **Bounded resources** — notification queue capped (drops instead of growing),
  connection rows de-duplicated and pruned, log rotation, request limits on
  paged endpoints, metrics label cardinality deliberately bounded.
- **Minimal dependencies** — four runtime dependencies; no shell execution
  libraries, no templating engines, no CDN assets; the dashboard ships its own
  CSS/JS.
- **Discovery surface** — the API is documented at `/api/docs`; it is a local
  tool, and obscurity is not a control, so this is a usability feature rather
  than a risk decision.
- **Secrets** — the SMTP password is read from `LNM_SMTP_PASSWORD`; config files
  that could contain credentials are git-ignored (`config.local.yaml`), and no
  secret is ever logged (the notifier logs recipients, not credentials).

---

## Things known and accepted

| Item | Why it is accepted |
|---|---|
| No authentication | Single-user local tool; mitigated by the loopback default and this policy |
| Monitoring data is readable by the local user | It is the local user's own data; file permissions are the OS's job |
| `<access_denied>` entries leak PID existence | The alternative is guessing names, which the project refuses to do |
| Process names/executable paths are stored in SQLite | Required for the product to answer "which process is using the network" |
| Logs may include endpoint addresses | Required for diagnosis; [RUNBOOK §9](docs/RUNBOOK.md) tells reporters to redact them |

---

## Supported versions

Security fixes target the latest release and `main`. Pre-1.0, the previous minor
line is supported only until the next one ships.

## Supply chain

Dependabot watches pip and GitHub Actions dependencies; CI runs `pip-audit`
against the runtime requirements; actions are pinned to major versions
(deliberate trade with maintenance overhead, documented in the CI workflow).
