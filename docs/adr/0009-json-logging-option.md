# ADR-0009: Text and JSON logging layouts

**Status:** Accepted · **Date:** 2026-10-04

## Context

Logs serve two audiences with opposite needs: a human reading a console while
starting the monitor, and a script or log shipper that must extract fields
("how many `COLLECTOR_FAILURE` events for the `connections` collector today?").
A single layout serves neither well.

## Decision

`logging.format: text | json`. The JSON formatter emits one object per line with
stable keys (`ts`, `level`, `logger`, `message`, `exception`) and passes through
contextual fields provided via `extra=`. No logging framework is added: this is
the standard library with two formatters.

## Consequences

- Operators get readable output by default; pipelines get parseable output by
  configuration.
- Contextual fields must be attached deliberately at call sites — they do not
  appear by magic. This is a feature: a field someone added on purpose is a field
  someone can query.
- Changing layouts is a config change, not a code change, so log-parsing jobs
  must be tolerant of both.

## Alternatives considered

- **`structlog` / `loguru`** — rejected: a dependency for what two classes do.
- **JSON only** — rejected: unreadable for the primary user in a terminal.
