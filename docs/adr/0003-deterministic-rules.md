# ADR-0003: Deterministic rules, not machine learning

**Status:** Accepted · **Date:** 2026-10-04

## Context

Every alert the monitor raises will be read by a person who wants to know *why*.
The specification forbids ML anomaly detection for the MVP and asks for
explainability ("WHAT happened? WHEN? WHY was it detected?").

## Decision

Six deterministic rules, each a pure function of a `RuleContext` producing
`Event` objects with structured `evidence`. Thresholds are configuration.
Detection state (baseline, seen PIDs, cooldown) lives in the engine, not in rules.

## Consequences

- Every event can be explained with the numbers that produced it — the evidence
  dict is the explanation, not a narrative added afterwards.
- Tuning is the operator's job and is explicit; the product will not learn that
  "18 Mb/s is normal here".
- New rules are cheap to add and trivially testable, but the rule set stays small
  on purpose: a rule nobody can explain is a rule nobody will trust.
- Baseline-based rules (`CONNECTION_SPIKE`) are naive compared with a seasonal
  model; accepted, since the failure mode is a missed event, not a wrong claim.

## Alternatives considered

- **Statistical anomaly detection (z-score on rolling windows)** — deferred: it
  produces alerts whose explanation is a formula, which fails the "explain it"
  test for the target user.
- **Signature/threat feeds** — out of scope: this is not a security product
  (SPECIFICATION §2.3).
