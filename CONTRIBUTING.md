# Contributing

Thanks for considering a contribution. This project measures other people's
machines and raises alerts they will act on, so the bar is *trustworthiness*
rather than feature count: a change that makes the product say something it
cannot prove will be rejected even if the code is good.

---

## 1. Ways to contribute

| Kind | Start here |
|---|---|
| Bug report | [Issue template](.github/ISSUE_TEMPLATE/bug_report.yml) — include `/api/status`, log excerpt, platform |
| Feature request | [Issue template](.github/ISSUE_TEMPLATE/feature_request.yml) — explain the question you want answered, not the widget you want built |
| Documentation | Typos, unclear procedures, missing runbook steps — all welcome; docs are part of the product |
| Code | Pick an issue labelled `status/good-first-issue`, or open one first for anything larger than a bug fix |
| Security | Do **not** open a public issue — see [SECURITY.md](SECURITY.md) |

Not in scope (see [SPECIFICATION.md §2.3](docs/SPECIFICATION.md)): packet capture,
DPI, ML anomaly detection, per-process byte accounting, multi-machine
aggregation, cloud deployment, auth, containerisation.

---

## 2. Development setup

```powershell
# Windows
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m pytest
```

```bash
# Linux / macOS
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

Common tasks are wrapped for both platforms:

| Task | PowerShell | Make |
|---|---|---|
| Setup | `.\scripts\dev.ps1 setup` | `make setup` |
| Tests | `.\scripts\dev.ps1 test` | `make test` |
| Tests + coverage | `.\scripts\dev.ps1 cov` | `make cov` |
| Lint | `.\scripts\dev.ps1 lint` | `make lint` |
| Format | `.\scripts\dev.ps1 format` | `make format` |
| All checks (as CI runs them) | `.\scripts\dev.ps1 check` | `make check` |
| Run the monitor | `.\scripts\dev.ps1 run` | `make run` |
| Build the package | `.\scripts\dev.ps1 build` | `make build` |

Pre-commit hooks (optional but recommended):

```bash
python -m pip install pre-commit
pre-commit install
```

---

## 3. Branching and commits

- Branches: `<type>/<short-topic>`, e.g. `feat/digest-email`, `fix/rate-reset`,
  `docs/runbook-storage`.
- Commits: [Conventional Commits](https://www.conventionalcommits.org/) —
  `feat(collectors): …`, `fix(api): …`, `docs(runbook): …`, `chore(ci): …`,
  `test(detection): …`. The scope should be a component name from
  [ARCHITECTURE.md §2](docs/ARCHITECTURE.md).
- One logical change per commit; the message explains **why**, the diff explains
  what. Layered commits are expected for a feature (e.g. model → storage →
  collector → tests).
- Rebase on `main` rather than merging it back in; keep history linear.

---

## 4. Tests are part of the change

Every change ships with tests at the level where it can actually fail:

| Change | Required tests |
|---|---|
| Rate/arithmetic, thresholds, parsing | Unit tests with controlled inputs (`test_metering.py`, `test_collectors.py`, `test_detection.py`) |
| Storage, schema, repositories | Round-trip and restart-persistence tests (`test_storage.py`) |
| API behaviour | Endpoint tests including error paths (`test_api.py`) |
| Loop, collectors, notifiers | Isolation/chaos tests (`test_monitor.py`, `test_fault_injection.py`) |
| Observability | `test_observability.py` (metrics parse, readiness semantics, JSON logs) |
| Configuration | `test_config.py` (defaults, overrides, rejection of bad values) |

Rules of thumb:

- If a bug was reported, the fix includes the test that would have caught it.
- Do not test private helpers for their own sake; test the contract that users
  (or the loop) depend on.
- Time-dependent tests use explicit timestamps; never `sleep` to make a race pass.

```bash
python -m pytest                 # everything
python -m pytest tests/test_detection.py -k spike
python -m pytest --cov=network_monitor --cov-report=term-missing
```

CI requires **coverage ≥ 85 %** (currently 88 %) and a green matrix on
Windows + Linux, Python 3.11 + 3.12.

---

## 5. Style and quality gates

- `ruff check` and `ruff format --check` must pass (config in `pyproject.toml`;
  line length 100).
- Type hints on public functions; `from __future__ import annotations` at the top
  of modules.
- Docstrings explain **why** a thing exists and what invariant it protects —
  not what the next line does.
- Broad `except Exception` is allowed only at documented isolation boundaries
  (the loop, notification dispatch, rule evaluation) and must record the failure
  somewhere visible.
- No new runtime dependency without justification (see
  [PLAN §5](docs/PLAN.md#5-dependency-policy)).
- Security-relevant patterns (`subprocess`, parsing untrusted output) need a
  comment explaining why they are safe; annotate intentional lint suppressions.

---

### Changing the dashboard

The dashboard is plain HTML/CSS/JS with no build step, so there is no compiler to
catch a script that queries an element the markup does not contain. Two tools
stand in for that:

```bash
python -m pytest tests/test_dashboard.py -q     # structural contract, runs in CI

python -m network_monitor --host 127.0.0.1 --port 8000 &
npm install --no-save jsdom                     # development-only, not a runtime dep
node scripts/dashboard-render-check.js          # renders the real page against live data
```

`tests/test_dashboard.py` asserts the HTML/JS element contract, the offline
invariant (no CDN, no remote fonts, relative API paths), table captions and scoped
headers, the token set, the loading/empty/error states, and the story order. If a
change reorders the page, update `STORY_SECTIONS` in that test deliberately.

`scripts/dashboard-render-check.js` loads the real page in jsdom, stubs the
canvas, feeds it payloads from a running monitor and checks what a person would
see — verdict, attention rows, process ranking, tables, the evidence dialog and
the theme toggle. It runs in CI as the `dashboard` job. Keep the design document
[docs/DASHBOARD.md](docs/DASHBOARD.md) and `CHANGELOG.md` in step with any
behavioural change: the design rules are part of the deliverable, not commentary
on it.

## 6. Definition of Done

A pull request is ready when:

- [ ] Tests added/updated and passing locally; CI green on both operating systems
- [ ] `ruff check` and `ruff format --check` clean
- [ ] User-visible behaviour documented (README, `/api/docs` via docstrings, or docs/)
- [ ] Operational impact recorded: [RUNBOOK.md](docs/RUNBOOK.md) for new failure
      modes, [SLO.md](docs/SLO.md) for new signals or targets
- [ ] `CHANGELOG.md` entry under `Unreleased`
- [ ] A design decision that is expensive to reverse is written up as an ADR
      (`docs/adr/`)
- [ ] No secrets, no generated data (`data/`, `logs/`), no debug leftovers

---

## 7. Reviewing

Reviewers look for, in order:

1. **Truthfulness** — does the change ever state something the data does not
   support? (per-process bytes, inferred process names, "healthy" while degraded)
2. **Failure behaviour** — what happens when the new code's dependency fails?
   Is the degradation visible in a probe, metric or event?
3. **Blast radius** — can this take down collection, the API, or notification
   delivery?
4. **Evidence** — is the claim in the PR description backed by a test or a
   measurement?
5. Style and naming last; tooling already covers most of it.

Review SLA: aim for first response within a few days. A PR with failing CI will
not be reviewed — fix the build first.

---

## 8. Releases

Maintainers cut releases when a milestone in [PLAN.md](docs/PLAN.md) closes:

1. Update `CHANGELOG.md` (move `Unreleased` → the version, dated).
2. Bump the version in `pyproject.toml` **and** `src/network_monitor/__init__.py`.
3. `git tag -a vX.Y.Z -m "vX.Y.Z"` and push the tag; the release workflow
   attaches the built wheel/sdist to the GitHub release.
4. Verify: install the artifact in a clean venv, run `--check-config` and `--once`.

Versioning is [SemVer](https://semver.org/): the public API is the REST surface
and the configuration file. Additive endpoints/options → minor; breaking
behaviour or config semantics → major (after 1.0).

---

## 9. Community

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
Questions about direction belong in an issue, not in a private message, so the
answer benefits the next contributor.
