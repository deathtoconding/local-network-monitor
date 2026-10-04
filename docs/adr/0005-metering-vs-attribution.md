# ADR-0005: Never claim per-process byte counts

**Status:** Accepted · **Date:** 2026-10-04

## Context

Users ask "how much did Chrome upload?". The MVP measures cumulative bytes per
**interface** (reliable, cheap) and connections with owning PIDs (reliable,
cheap) — but nothing in that pipeline can attribute bytes to a process. Windows
offers ETW and per-socket counters; neither is in the MVP's mechanism list.

## Decision

Interface metering and connection attribution are separate data products with
separate labels in the API and dashboard. No code path multiplies or divides one
by the other. `ProcessInfo`/`NetworkConnection` never carry byte counters.

## Consequences

- The product cannot answer "how many MB did this process transfer", and says so
  in the dashboard footnote rather than guessing. Trustworthiness over apparent
  completeness.
- Adding real per-process volume later is an additive feature (a new collector
  and a new field), not a rewrite — but it must never be approximated from
  connection counts.
- Any PR that introduces a per-process byte claim is rejected by design.

## Alternatives considered

- **Estimate by sampling** — rejected: an estimate presented as a measurement is a
  lie with a confidence interval.
- **`psutil.Process.io_counters()`** — measures disk (and on Linux, socket I/O in
  aggregate), not per-connection network traffic; it does not answer the question.
