# ADR-0007: Store connection snapshots only on change

**Status:** Accepted · **Date:** 2026-10-04

## Context

The TCP table is collected every second. Writing it unconditionally produced
~3 700 rows in 11 minutes on an idle sandbox — tens of millions of rows over a
7-day window, for information that did not change between cycles.

## Decision

A snapshot is written only when the identity set of connections differs from the
last stored snapshot. The live view is unaffected (it uses memory). Connection
rows are pruned on a shorter window (`connection_retention_hours`, default 24 h)
than measurements and events.

## Consequences

- Idle machines write almost nothing; busy machines still write on churn, which is
  exactly the information worth keeping.
- The database answers "what connections were observed and when they changed",
  which is a better history than a 1 Hz re-recording of the same rows.
- Row-level history is not a complete 1 Hz record; retention duration inference
  from snapshots is approximate. Accepted and documented.
- Identity is `(protocol, local addr/port, remote addr/port, state, pid)`;
  changes in state (e.g. `ESTABLISHED → TIME_WAIT`) count as changes and are kept.

## Alternatives considered

- **Write at a lower cadence** — rejected: still writes identical rows, just fewer.
- **Keep only the latest snapshot** — rejected: loses the "what happened" trail
  the product exists to provide.
