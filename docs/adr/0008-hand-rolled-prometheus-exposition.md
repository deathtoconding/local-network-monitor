# ADR-0008: Hand-rolled Prometheus exposition

**Status:** Accepted · **Date:** 2026-10-04

## Context

Operability requires metrics. `prometheus_client` is the obvious library, but the
project has four runtime dependencies and a stated policy of justifying each one
(PLAN §5).

## Decision

Implement the text exposition format (v0.0.4) in ~200 lines of dependency-free
code (`api/metrics.py`), with bounded label cardinality, correct HELP/TYPE blocks,
label escaping and unit-suffixed names.

## Consequences

- No new runtime dependency; the format is simple, stable and specified.
- Tests assert the format is parseable, that HELP/TYPE precede every family, and
  that no unbounded label (process name, remote address) can ever appear.
- Features of `prometheus_client` are absent: no pushgateway, no custom
  collectors, no multiprocess aggregation, no histograms (only latest and max
  cycle duration). If a real histogram is ever needed, revisit this decision.
- Someone must remember the format rules; the tests are that someone.

## Alternatives considered

- **`prometheus_client`** — viable and would be justified *if* the project needed
  histograms or a registry; not justified for fourteen gauges and counters.
- **StatsD/Graphite** — no local consumer exists; text exposition is scrapeable by
  every mainstream agent.
