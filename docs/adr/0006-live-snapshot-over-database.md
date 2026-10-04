# ADR-0006: Live snapshot preferred over database reads

**Status:** Accepted · **Date:** 2026-10-04

## Context

The API must report the current state. Two sources exist: the in-memory snapshot
published by the loop, and rows in SQLite written over time. A naive
"if in-memory is empty, read the database" rule produced a concrete bug: with
zero active connections, the API served *stale* rows from the last time there
were some, so the dashboard showed connections that no longer existed.

## Decision

While the loop is running (`cycle_count > 0`), reads come from the in-memory
snapshot — including when it is empty. Only `--api-only` mode, or a process that
has not completed a cycle yet, reads history back from SQLite. Connection
snapshots are still persisted as history.

## Consequences

- "No connections right now" is expressible and truthful.
- The API never performs a write-path query for current state, so request latency
  does not depend on disk.
- Two modes of serving exist and must both be tested (`--api-only` tests).
- History queries (`/api/traffic/history`, `/api/events`) still come from the
  database, where they belong.

## Alternatives considered

- **Always read the database** — rejected: correctness bug above, plus a disk read
  per request on a hot path.
- **Never read the database** — rejected: `--api-only` over existing data is a
  genuinely useful mode (inspect last night's run without restarting collection).
