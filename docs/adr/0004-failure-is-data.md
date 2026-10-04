# ADR-0004: A collector failure is data, not an exception

**Status:** Accepted · **Date:** 2026-10-04

## Context

Collection depends on things that fail routinely on real machines: PowerShell
policies, non-elevated sessions, AV/EDR hooks, sleeping hosts, transient locks.
The specification's reliability requirement is explicit: one failed collector must
not stop the monitor.

## Decision

Collectors raise `CollectorError`; the loop catches **everything**, records the
outcome in the collector health registry (state, last success, last error,
consecutive failures), never aborts the cycle, and lets the
`COLLECTOR_FAILURE` rule turn prolonged failure into an event.

## Consequences

- Partial data is normal and is labelled: `<access_denied>` in a process column,
  `degraded` in a collector state, `failed_cycles_total` in metrics.
- Degradation is always visible in an existing signal; silent degradation is the
  only unacceptable outcome (SLO-8 makes it a measurable promise).
- Bugs inside collectors surface as data-quality problems rather than crashes —
  which is why the health registry and `/api/ready` must be part of any review.
- Broad `except Exception` is intentional and annotated; it is the isolation
  boundary, not laziness.

## Alternatives considered

- **Fail fast** — rejected: a monitor that stops because PowerShell is slow is
  worse than useless.
- **Retry with backoff inside the loop** — rejected for the MVP: the loop *is* the
  retry (1 Hz), and a retry loop would eat the collection interval.
