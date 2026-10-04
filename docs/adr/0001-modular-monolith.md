# ADR-0001: Modular monolith instead of services

**Status:** Accepted · **Date:** 2026-10-04

## Context

The specification requires collectors, storage, detection, API and notifications,
which is enough separation to tempt a service-per-component layout. The deployment
target is, however, a single Windows machine used by one person.

## Decision

One process, one repository, modules with explicit boundaries:
`collectors → storage/detection → api`, dependency direction enforced by module
layout and import discipline. Cross-module communication is by function call and
in-process queues only.

## Consequences

- Deployment is `pip install` + one command; no service discovery, no ports to
  open, no orchestration.
- Failure isolation is achieved with try/except boundaries and per-component
  health records, **not** with process isolation. A crash of the API process
  takes collection down with it (documented in RUNBOOK §1).
- One database connection shared by three threads, protected by a lock
  (ARCHITECTURE §4) — a trade accepted for the simplicity of one file.
- Scaling out is not a supported path; scaling *up* means more frequent cycles.

## Alternatives considered

- **Microservices/containers** — rejected: violates the single-machine target,
  and a container's network namespace would measure the wrong thing.
- **Separate collector and API processes** — rejected: two processes on a
  home machine is two things to supervise, restart and log.
- **Thread-per-collector first** — rejected initially; the loop is fast enough and
  sequential execution keeps the timing model obvious. Revisit if a future
  collector (e.g. Performance Counters) blocks the cycle.
