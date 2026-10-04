# Documentation index

Start here depending on what you are doing.

| If you want to… | Read |
|---|---|
| Install, run and understand the product | [README.md](../README.md) |
| Understand **why** the system is shaped this way | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Know what was specified and what was actually built | [SPECIFICATION.md](SPECIFICATION.md) |
| Know what "healthy" means numerically | [SLO.md](SLO.md) |
| Fix something that is broken right now | [RUNBOOK.md](RUNBOOK.md) |
| See what is coming next and how work is managed | [PLAN.md](PLAN.md) |
| Know why a decision was made | [adr/](adr/README.md) |
| Contribute code or docs | [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Report a vulnerability | [SECURITY.md](../SECURITY.md) |
| See what changed and when | [CHANGELOG.md](../CHANGELOG.md) |

## How these documents fit together

```
SPECIFICATION ──► what must exist (and the MVP boundary)
      │
      ├──► ARCHITECTURE ──► how it is built, and where failure lands
      │          │
      │          └──► adr/ ──► why each expensive decision was taken
      │
      ├──► SLO ──────────► what healthy means, with targets and error budget
      │          │
      │          └──► RUNBOOK ──► what to do when a target is missed
      │
      └──► PLAN ─────────► what is next, in what order, and the rules for it
```

Documents are part of the deliverable, not commentary on it: a change that alters
behaviour, signals or failure modes updates the relevant document in the same pull
request (see [CONTRIBUTING.md §6](../CONTRIBUTING.md)).
