"""Application entry point and CLI.

Usage examples (Windows PowerShell):

    python -m network_monitor                  # dashboard on http://127.0.0.1:8000
    python -m network_monitor --once           # one collection cycle, printed, then exit
    python -m network_monitor --api-only       # serve the API over existing data only
    python -m network_monitor --config config.local.yaml
    python -m network_monitor --check-config

The process runs the monitoring loop on a background thread and uvicorn on the
main thread, so Ctrl+C shuts both down cleanly.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Any, Optional, Sequence

from . import __version__
from .api import create_app
from .config import Config, ConfigError, load_config
from .logging_setup import configure_logging
from .monitor import Monitor

BANNER_WIDTH = 52


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-monitor",
        description="Local Network Monitor - measure, explain and display local network activity.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="path to a YAML configuration file (default: ./config.yaml when present)",
    )
    parser.add_argument("--host", default=None, help="dashboard bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="dashboard TCP port (default 8000)")
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between collection cycles (default 1.0)",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="serve the dashboard without running collectors",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single collection cycle, print a summary and exit",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="override the configured log level",
    )
    parser.add_argument(
        "--check-config", action="store_true", help="validate configuration and exit"
    )
    parser.add_argument("--version", action="version", version=f"local-network-monitor {__version__}")
    return parser


def load_effective_config(args: argparse.Namespace) -> Config:
    """Load the config file and apply command-line overrides."""
    config = load_config(args.config)
    if args.host:
        config.monitor.host = args.host
    if args.port:
        config.monitor.port = args.port
    if args.interval:
        config.monitor.collection_interval = args.interval
    if args.log_level:
        config.logging.level = args.log_level
    return config


def print_banner(config: Config, status: str = "RUNNING") -> None:
    url = f"http://{config.monitor.host}:{config.monitor.port}"
    print("=" * BANNER_WIDTH)
    print(f" Local Network Monitor v{__version__}")
    print("=" * BANNER_WIDTH)
    print(f" Status:       {status}")
    print(f" Dashboard:    {url}")
    print(f" API docs:     {url}/api/docs")
    print(f" Interval:     {config.monitor.collection_interval}s")
    print(f" Database:     {config.database.path}")
    print(f" Log file:     {config.logging.file}")
    print(f" Config:       {config.source_path or 'built-in defaults'}")
    print("=" * BANNER_WIDTH)
    sys.stdout.flush()


def run_single_cycle(config: Config) -> int:
    """``--once``: one cycle, human-readable summary. Returns an exit code."""
    monitor = Monitor(config)
    print_banner(config, status="RUNNING (single cycle)")
    events = monitor.cycle()
    state = monitor.state
    traffic = state.traffic_snapshot()

    print()
    print("Traffic")
    print("-" * BANNER_WIDTH)
    for measurement in state.current_measurements():
        upload = (measurement.upload_rate or 0.0) / 125_000
        download = (measurement.download_rate or 0.0) / 125_000
        print(
            f" {measurement.interface_name:<28} down {download:8.3f} Mb/s   up {upload:8.3f} Mb/s"
        )
    if not state.current_measurements():
        print(" (no interface measurements - first cycle only establishes baselines)")

    connections = state.current_connections()
    print()
    print("Connections")
    print("-" * BANNER_WIDTH)
    print(f" Active TCP connections: {len(connections)}")
    for connection in connections[:15]:
        name = connection.process_name or (
            f"PID {connection.pid}" if connection.pid is not None else "unknown"
        )
        print(
            f" {name:<28} {connection.state:<12} {connection.local_endpoint} -> "
            f"{connection.remote_endpoint}"
        )
    if len(connections) > 15:
        print(f" ... and {len(connections) - 15} more")

    print()
    print("Collectors")
    print("-" * BANNER_WIDTH)
    for name, status in sorted(state.health.statuses.items()):
        detail = f" ({status.last_error})" if status.last_error else ""
        print(f" {name:<28} {status.state}{detail}")

    print()
    print(f" Events this cycle: {len(events)}")
    for event in events:
        print(f"  - {event.summary_line()}")
    print()
    print(f" Totals: down {traffic.download_bytes_per_second:,.0f} B/s  "
          f"up {traffic.upload_bytes_per_second:,.0f} B/s")
    print(f" Stored in {config.database.path}")
    state.database.close()
    return 0


def run_dashboard(
    config: Config,
    *,
    api_only: bool = False,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Start the monitor (unless ``api_only``) and serve the dashboard."""
    import uvicorn

    monitor: Optional[Monitor] = None
    state: Any = None
    if api_only:
        from .api import build_state

        state = build_state(config, with_notifications=False)
    else:
        monitor = Monitor(config)
        state = monitor.state

    app = create_app(state=state, config=config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.monitor.host,
            port=config.monitor.port,
            log_config=None,  # keep our own logging configuration
            log_level=config.logging.level.lower(),
            access_log=False,
            server_header=False,
        )
    )

    def request_shutdown(*_args: object) -> None:
        logger = logging.getLogger(__name__)
        logger.info("shutdown requested")
        server.should_exit = True

    previous_handlers: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, request_shutdown)
        except (ValueError, OSError):  # pragma: no cover - non-main thread / Windows edge
            pass

    print_banner(config, status="RUNNING (API only)" if api_only else "RUNNING")
    if monitor is not None:
        monitor.start()

    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - depends on the terminal
        pass
    finally:
        if monitor is not None:
            monitor.stop()
        if state is not None:
            state.database.close()
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, TypeError):  # pragma: no cover
                pass

    logger = logging.getLogger(__name__)
    logger.info("Local Network Monitor stopped")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    try:
        config = load_effective_config(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.logging)
    logger = logging.getLogger(__name__)
    logger.info("Local Network Monitor v%s starting", __version__)

    if args.check_config:
        print("Configuration OK")
        print(f"  config file: {config.source_path or '(built-in defaults)'}")
        print(f"  database:    {config.database.path}")
        print(f"  interval:    {config.monitor.collection_interval}s")
        print(f"  dashboard:   http://{config.monitor.host}:{config.monitor.port}")
        print(
            "  thresholds:  download "
            f"{config.detection.download_threshold_mbps} Mb/s, upload "
            f"{config.detection.upload_threshold_mbps} Mb/s"
        )
        return 0

    if args.once:
        try:
            return run_single_cycle(config)
        except Exception:  # noqa: BLE001 - report and fail like a CLI should
            logger.exception("single-cycle run failed")
            return 1

    try:
        return run_dashboard(config, api_only=args.api_only)
    except OSError as exc:
        print(f"cannot start dashboard on {config.monitor.host}:{config.monitor.port}: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("fatal error")
        return 1


def run() -> None:
    """Console-script wrapper (``network-monitor``)."""
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
