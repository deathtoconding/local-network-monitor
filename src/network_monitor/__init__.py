"""Local Network Monitor.

A single-machine, modular-monolith network monitor for Windows: it measures
interface traffic, correlates TCP connections with owning processes, applies
deterministic detection rules, persists everything in SQLite and exposes the
result through a local FastAPI dashboard.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
