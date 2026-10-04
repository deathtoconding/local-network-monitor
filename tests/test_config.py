"""Configuration tests.

Configuration is the control surface for every threshold and retention policy,
so it gets the same treatment as the code it governs: defaults must be sane,
explicit values must win, and mistakes must fail loudly instead of silently
disabling a rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from network_monitor.config import (
    Config,
    ConfigError,
    DatabaseSection,
    DetectionSection,
    EmailSection,
    LoggingSection,
    MonitorSection,
    NotificationsSection,
    load_config,
)


class TestDefaults:
    def test_defaults_match_the_documented_config(self):
        config = Config()
        assert config.monitor.collection_interval == 1.0
        assert config.monitor.host == "127.0.0.1"  # never public by default
        assert config.monitor.port == 8000
        assert config.database.retention_days == 7
        assert config.database.connection_retention_hours == 24
        assert config.detection.download_threshold_mbps == 10
        assert config.detection.upload_threshold_mbps == 5
        assert config.notifications.enabled is False
        assert config.logging.format == "text"
        assert config.source_path is None

    def test_repository_config_yaml_is_valid(self):
        """The shipped config.yaml must load cleanly and stay in sync."""
        repository_root = Path(__file__).resolve().parents[1]
        config = load_config(repository_root / "config.yaml")
        assert config.source_path is not None
        assert config.detection.download_threshold_mbps > 0
        assert config.logging.format in {"text", "json"}

    def test_thresholds_convert_to_bytes_per_second(self):
        detection = DetectionSection(download_threshold_mbps=10, upload_threshold_mbps=5)
        assert detection.download_threshold_bytes_per_second == 1_250_000
        assert detection.upload_threshold_bytes_per_second == 625_000


class TestLoading:
    def test_values_override_defaults(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text(
            """
monitor:
  collection_interval: 2.5
  port: 9100
detection:
  download_threshold_mbps: 50
logging:
  format: json
""",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.monitor.collection_interval == 2.5
        assert config.monitor.port == 9100
        assert config.monitor.host == "127.0.0.1"  # untouched default survives
        assert config.detection.download_threshold_mbps == 50
        assert config.logging.format == "json"
        assert config.source_path == str(path)

    def test_no_config_file_anywhere_uses_defaults(self, tmp_path: Path, monkeypatch):
        """Starting in an empty directory must just work on built-in defaults."""
        monkeypatch.chdir(tmp_path)
        config = load_config()
        assert config.monitor.port == 8000
        assert config.source_path is None

    def test_explicitly_missing_file_is_an_error(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nope.yaml")

    def test_empty_file_is_valid(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("", encoding="utf-8")
        assert load_config(path).monitor.port == 8000

    def test_broken_yaml_is_reported(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("monitor: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="cannot read config file"):
            load_config(path)

    def test_non_mapping_document_is_rejected(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="must contain a mapping"):
            load_config(path)

    def test_unknown_section_is_rejected(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text("detecton:\n  download_threshold_mbps: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown configuration section"):
            load_config(path)

    def test_unknown_option_is_rejected(self, tmp_path: Path):
        """A typo must never silently disable a rule."""
        path = tmp_path / "config.yaml"
        path.write_text("detection:\n  download_threshold: 1\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="unknown option"):
            load_config(path)

    def test_nested_email_section_is_loaded(self, tmp_path: Path):
        path = tmp_path / "config.yaml"
        path.write_text(
            """
notifications:
  enabled: true
  min_severity: critical
  email:
    enabled: true
    smtp_host: smtp.example.com
    sender: monitor@example.com
    recipients: [ops@example.com]
""",
            encoding="utf-8",
        )
        config = load_config(path)
        assert config.notifications.email.smtp_host == "smtp.example.com"
        assert config.notifications.email.recipients == ["ops@example.com"]
        assert config.notifications.email.is_usable()
        assert config.notifications.min_severity == "critical"

    def test_severity_is_normalised(self):
        section = NotificationsSection(min_severity="WARNING")
        assert section.min_severity == "warning"

    def test_invalid_severity_is_rejected(self):
        with pytest.raises(ConfigError, match="min_severity"):
            NotificationsSection(min_severity="loud")

    def test_to_dict_is_serialisable(self):
        import json

        # No non-JSON types may leak out: this dict is logged and served.
        assert json.dumps(Config().to_dict())


class TestValidation:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"collection_interval": 0.0}, "collection_interval"),
            ({"port": 0}, "port"),
            ({"port": 99999}, "port"),
        ],
    )
    def test_monitor_section_rejects_bad_values(self, kwargs, message):
        with pytest.raises(ConfigError, match=message):
            MonitorSection(**kwargs)

    def test_thresholds_cannot_be_negative(self):
        with pytest.raises(ConfigError, match="thresholds"):
            DetectionSection(download_threshold_mbps=-1)

    def test_spike_multiplier_must_be_at_least_one(self):
        with pytest.raises(ConfigError, match="connection_spike_multiplier"):
            DetectionSection(connection_spike_multiplier=0.5)

    def test_collector_timeout_must_be_positive(self):
        with pytest.raises(ConfigError, match="collector_failure_timeout_seconds"):
            DetectionSection(collector_failure_timeout_seconds=0)

    def test_logging_level_and_format_are_validated(self):
        with pytest.raises(ConfigError, match="logging.level"):
            LoggingSection(level="chatty")
        with pytest.raises(ConfigError, match="logging.format"):
            LoggingSection(format="xml")

    def test_prune_interval_has_a_floor(self):
        with pytest.raises(ConfigError, match="prune_interval_seconds"):
            DatabaseSection(prune_interval_seconds=1)

    def test_retention_windows_cannot_be_negative(self):
        with pytest.raises(ConfigError, match="retention_days"):
            DatabaseSection(retention_days=-1)
        with pytest.raises(ConfigError, match="connection_retention_hours"):
            DatabaseSection(connection_retention_hours=-1)


class TestInterfaceFiltering:
    def test_excluded_interfaces_match_case_insensitively(self):
        section = MonitorSection(exclude_interfaces=["Loopback Pseudo-Interface 1", "lo"])
        assert section.is_excluded("LO")
        assert section.is_excluded("loopback pseudo-interface 1")
        assert not section.is_excluded("Ethernet")


class TestSecrets:
    def test_environment_password_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("LNM_SMTP_PASSWORD", "from-env")
        assert EmailSection(password="from-file").resolved_password() == "from-env"

    def test_config_password_is_used_without_environment(self, monkeypatch):
        monkeypatch.delenv("LNM_SMTP_PASSWORD", raising=False)
        assert EmailSection(password="from-file").resolved_password() == "from-file"

    def test_email_needs_host_sender_and_recipients_to_be_usable(self):
        assert not EmailSection(enabled=True).is_usable()
        usable = EmailSection(enabled=True, smtp_host="smtp", sender="a@b.c", recipients=["x@y.z"])
        assert usable.is_usable()
