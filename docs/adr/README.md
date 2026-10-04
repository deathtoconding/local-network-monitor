# Architecture Decision Records

Short records of decisions that are expensive to reverse. Format:
**context → decision → consequences → alternatives considered.** Each one names
what it forbids, because a decision that forbids nothing is not a decision.

| # | Title | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith instead of services | Accepted |
| [0002](0002-sqlite-primary-store.md) | SQLite as the primary store | Accepted |
| [0003](0003-deterministic-rules.md) | Deterministic rules, not machine learning | Accepted |
| [0004](0004-failure-is-data.md) | A collector failure is data, not an exception | Accepted |
| [0005](0005-metering-vs-attribution.md) | Never claim per-process byte counts | Accepted |
| [0006](0006-live-snapshot-over-database.md) | Live snapshot preferred over database reads | Accepted |
| [0007](0007-store-connection-snapshots-on-change.md) | Store connection snapshots only on change | Accepted |
| [0008](0008-hand-rolled-prometheus-exposition.md) | Hand-rolled Prometheus exposition | Accepted |
| [0009](0009-json-logging-option.md) | Text and JSON logging layouts | Accepted |

## Adding a record

1. Copy the structure of the most recent record.
2. Number it sequentially; never renumber.
3. Link it from this index and from [ARCHITECTURE.md §13](../ARCHITECTURE.md#13-decision-log).
4. Superseding a record means writing a new one and marking the old one
   `Superseded by ADR-XXXX` — the old text stays for history.
