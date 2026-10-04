"""Centralised configuration.

All tunables live here so that no other module reads magic numbers out of the
environment or hard-codes thresholds. Configuration is layered:

1. built-in defaults (this file)
2. the YAML file passed with ``--config`` (``config.yaml`` by default)
3. a handful of environment variables (secrets, chiefly SMTP passwords)

Nothing in the repository *requires* a config file: the application starts with
the defaults below and only the file's overrides are applied.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Type, TypeVar

import yaml

T = TypeVar("T")

DEFAULT_CONFIG_FILENAME = "config.yaml"
ENV_PREFIX = "LNM_"


class ConfigError(RuntimeError):
    """Raised when configuration cannot be read or is not valid."""


@dataclass
class MonitorSection:
    """Runtime loop and web server settings."""

    collection_interval: float = 1.0
    host: str = "127.0.0.1"
    port: int = 8000
    exclude_interfaces: List[str] = field(
        default_factory=lambda: ["Loopback Pseudo-Interface 1", "lo"]
    )

    def __post_init__(self) -> None:
        if self.collection_interval < 0.1:
            raise ConfigError("monitor.collection_interval must be >= 0.1 seconds")
        if not (0 < self.port < 65536):
            raise ConfigError("monitor.port must be a valid TCP port")

    def is_excluded(self, interface_name: str) -> bool:
        """Return True when an interface name is on the ignore list."""
        lowered = {name.lower() for name in self.exclude_interfaces}
        return interface_name.lower() in lowered


@dataclass
class DatabaseSection:
    """SQLite storage settings."""

    path: str = "data/monitor.db"
    #: How long interface measurements and events are kept.
    retention_days: int = 7
    #: Connection snapshots are far more numerous than measurements, so they
    #: get a shorter window. 0 disables connection pruning.
    connection_retention_hours: int = 24
    prune_interval_seconds: int = 3600

    @property
    def path_obj(self) -> Path:
        return Path(self.path).expanduser()

    def __post_init__(self) -> None:
        if self.retention_days < 0:
            raise ConfigError("database.retention_days must be >= 0")
        if self.connection_retention_hours < 0:
            raise ConfigError("database.connection_retention_hours must be >= 0")
        if self.prune_interval_seconds < 60:
            raise ConfigError("database.prune_interval_seconds must be >= 60")


@dataclass
class DetectionSection:
    """Thresholds and behaviour of the deterministic detection rules."""

    download_threshold_mbps: float = 10.0
    upload_threshold_mbps: float = 5.0
    connection_spike_multiplier: float = 3.0
    connection_spike_min_baseline: int = 10
    collector_failure_timeout_seconds: float = 15.0
    event_cooldown_seconds: float = 60.0
    seed_processes_on_start: bool = True
    new_process_rule_enabled: bool = True

    #: 1 megabit/s expressed in bytes per second (decimal, as used by ISPs).
    BYTES_PER_MEGABIT: int = 125_000

    @property
    def download_threshold_bytes_per_second(self) -> float:
        """Download threshold converted from Mb/s to bytes/second."""
        return self.download_threshold_mbps * self.BYTES_PER_MEGABIT

    @property
    def upload_threshold_bytes_per_second(self) -> float:
        """Upload threshold converted from Mb/s to bytes/second."""
        return self.upload_threshold_mbps * self.BYTES_PER_MEGABIT

    def __post_init__(self) -> None:
        if self.download_threshold_mbps < 0 or self.upload_threshold_mbps < 0:
            raise ConfigError("detection thresholds must be >= 0")
        if self.connection_spike_multiplier < 1:
            raise ConfigError("detection.connection_spike_multiplier must be >= 1")
        if self.connection_spike_min_baseline < 1:
            raise ConfigError("detection.connection_spike_min_baseline must be >= 1")
        if self.collector_failure_timeout_seconds <= 0:
            raise ConfigError("detection.collector_failure_timeout_seconds must be > 0")
        if self.event_cooldown_seconds < 0:
            raise ConfigError("detection.event_cooldown_seconds must be >= 0")


@dataclass
class EmailSection:
    """SMTP settings for the email notifier."""

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    password: str = ""
    sender: str = ""
    recipients: List[str] = field(default_factory=list)

    def resolved_password(self) -> str:
        """Password from the environment wins over the config file."""
        return os.environ.get(f"{ENV_PREFIX}SMTP_PASSWORD", "") or self.password

    def is_usable(self) -> bool:
        return bool(self.enabled and self.smtp_host and self.sender and self.recipients)


@dataclass
class NotificationsSection:
    """Notification dispatch settings."""

    enabled: bool = False
    min_severity: str = "warning"
    cooldown_seconds: float = 300.0
    email: EmailSection = field(default_factory=EmailSection)

    def __post_init__(self) -> None:
        allowed = {"info", "warning", "critical"}
        if self.min_severity.lower() not in allowed:
            raise ConfigError(f"notifications.min_severity must be one of {sorted(allowed)}")
        self.min_severity = self.min_severity.lower()


@dataclass
class LoggingSection:
    """Application logging settings."""

    level: str = "INFO"
    #: "text" for humans reading a console, "json" for log shippers and alerting.
    format: str = "text"
    file: str = "logs/monitor.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    console: bool = True

    def __post_init__(self) -> None:
        self.level = self.level.upper()
        valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if self.level not in valid:
            raise ConfigError(f"logging.level must be one of {sorted(valid)}")
        self.format = self.format.lower()
        if self.format not in {"text", "json"}:
            raise ConfigError("logging.format must be 'text' or 'json'")


@dataclass
class Config:
    """Root configuration object passed to every component."""

    monitor: MonitorSection = field(default_factory=MonitorSection)
    database: DatabaseSection = field(default_factory=DatabaseSection)
    detection: DetectionSection = field(default_factory=DetectionSection)
    notifications: NotificationsSection = field(default_factory=NotificationsSection)
    logging: LoggingSection = field(default_factory=LoggingSection)
    #: Path of the file this configuration was loaded from (None = defaults).
    source_path: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict view of the configuration (useful for logs and the API)."""
        return asdict(self)


def _assign_section(section_cls: Type[T], values: Mapping[str, Any], name: str) -> T:
    """Instantiate a dataclass section, rejecting unknown keys."""
    if not isinstance(values, Mapping):
        raise ConfigError(f"configuration section '{name}' must be a mapping")

    known = {f.name for f in fields(section_cls)}  # type: ignore[arg-type]
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"unknown option(s) in '{name}': {', '.join(sorted(unknown))}")

    kwargs: Dict[str, Any] = {}
    for key, value in values.items():
        fld = next(f for f in fields(section_cls) if f.name == key)  # type: ignore[arg-type]
        if is_dataclass(fld.type) and isinstance(value, Mapping):
            kwargs[key] = _assign_section(fld.type, value, f"{name}.{key}")  # type: ignore[arg-type]
        else:
            kwargs[key] = value
    return section_cls(**kwargs)  # type: ignore[call-arg]


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from ``path`` merged over the built-in defaults.

    A missing file is not an error when no explicit path was requested: the
    application then simply runs on its defaults.
    """
    config_path = Path(path) if path is not None else Path.cwd() / DEFAULT_CONFIG_FILENAME

    raw: Dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, Mapping):
            raise ConfigError(f"config file {config_path} must contain a mapping")
        raw = dict(loaded)
    elif path is not None:
        raise ConfigError(f"config file not found: {config_path}")

    known_sections = {"monitor", "database", "detection", "notifications", "logging"}
    unknown = set(raw) - known_sections
    if unknown:
        raise ConfigError(f"unknown configuration section(s): {', '.join(sorted(unknown))}")

    email_raw = raw.get("notifications", {}).get("email", {})
    notifications = _assign_section(
        NotificationsSection,
        {k: v for k, v in raw.get("notifications", {}).items() if k != "email"},
        "notifications",
    )
    notifications.email = _assign_section(EmailSection, email_raw, "notifications.email")

    return Config(
        monitor=_assign_section(MonitorSection, raw.get("monitor", {}), "monitor"),
        database=_assign_section(DatabaseSection, raw.get("database", {}), "database"),
        detection=_assign_section(DetectionSection, raw.get("detection", {}), "detection"),
        notifications=notifications,
        logging=_assign_section(LoggingSection, raw.get("logging", {}), "logging"),
        source_path=str(config_path) if config_path.is_file() else None,
    )
