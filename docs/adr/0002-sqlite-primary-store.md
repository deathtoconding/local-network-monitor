# ADR-0002: SQLite as the primary store

**Status:** Accepted · **Date:** 2026-10-04

## Context

History must survive restarts, and the specification requires querying by time,
interface, process, TCP state and event type. The host is a single machine with a
consumer disk.

## Decision

SQLite in WAL mode, one file (`data/monitor.db`), schema created on first start
and versioned with `PRAGMA user_version`. All SQL lives in
`storage/repositories.py`.

## Consequences

- Zero installation surface; backup is a file copy; recovery procedures fit on one
  page (RUNBOOK §4).
- Readers (HTTP requests) do not block the writer (loop) thanks to WAL.
- Concurrent multi-process writers are not supported; a second monitor instance
  against the same file is a misconfiguration, not a feature.
- Time-series volume is managed by retention, not by partitioning.
- Aggregation across machines is impossible without an export step — by design.

## Alternatives considered

- **PostgreSQL** — rejected by the specification and by operational reality: a
  database server on a home machine is a liability, not a feature.
- **JSON lines on disk** — honestly adequate for the volume, but loses indexed
  queries, filtering and atomic upserts.
- **In-memory only** — rejected: violates "restart must not delete measurements".
