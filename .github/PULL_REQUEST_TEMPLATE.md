<!--
Thanks for contributing. Keep this short and concrete: what changes, why, and how
you know it works. Delete the sections that do not apply.
-->

## What and why

<!-- The problem, then the change. Link the issue: Closes #123 -->

## How it was verified

<!--
Tests added/updated, commands run, and any manual check. Paste the observed
output rather than describing it: "247 passed" beats "tests pass".
-->

```
python -m pytest
python -m ruff check src tests && python -m ruff format --check src tests
```

## Operational impact

<!--
Does this change a signal, a threshold, a failure mode or a probe? If yes, say
what an operator will now see differently.
-->

- [ ] No change to observable behaviour, SLIs, or failure modes
- [ ] `/api/status`, `/api/metrics` or `/api/ready` output changed → documented
- [ ] New/changed failure mode → [RUNBOOK.md](../docs/RUNBOOK.md) updated
- [ ] New/changed signal or target → [SLO.md](../docs/SLO.md) updated
- [ ] Decision that is expensive to reverse → ADR added under `docs/adr/`

## Truthfulness check

<!--
The project refuses to state things it cannot prove. Confirm the change does not
introduce an unprovable claim (per-process byte counts, guessed process names,
"healthy" while degraded).
-->

- [ ] Every number this change exposes is measured, not estimated
- [ ] Failures are recorded somewhere visible (log, metric, probe, or event)

## Checklist

- [ ] Tests added/updated; CI green
- [ ] `CHANGELOG.md` updated under `Unreleased`
- [ ] Docs updated (README / docstrings / `docs/`)
- [ ] No secrets, generated data (`data/`, `logs/`) or debug leftovers
