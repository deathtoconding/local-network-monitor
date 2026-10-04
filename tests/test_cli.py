"""CLI tests.

The command line is the primary interface a human uses to run and diagnose the
monitor, so it is tested like one: flags parse, `--check-config` validates,
`--once` produces a report a human can read, and bad input exits with a code
that a script can branch on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from network_monitor import __version__
from network_monitor.main import build_parser, load_effective_config, main, print_banner

CONFIG_TEMPLATE = """
monitor:
  collection_interval: 1.0
  host: 127.0.0.1
  port: 8123
  exclude_interfaces: ["lo"]
database:
  path: {database}
detection:
  download_threshold_mbps: 10
logging:
  file: {log}
  console: false
"""


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(database=tmp_path / "cli.db", log=tmp_path / "cli.log"),
        encoding="utf-8",
    )
    return path


class TestArgumentParsing:
    def test_defaults(self):
        args = build_parser().parse_args([])
        assert args.config is None
        assert args.once is False
        assert args.api_only is False
        assert args.check_config is False

    def test_flags_are_parsed(self):
        args = build_parser().parse_args(
            [
                "--config",
                "x.yaml",
                "--port",
                "9001",
                "--once",
                "--interval",
                "2.5",
                "--log-level",
                "DEBUG",
            ]
        )
        assert args.config == "x.yaml"
        assert args.port == 9001
        assert args.once is True
        assert args.interval == 2.5
        assert args.log_level == "DEBUG"

    def test_version_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--version"])
        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out

    def test_unknown_flag_fails_loudly(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args(["--nonsense"])
        assert excinfo.value.code == 2


class TestEffectiveConfiguration:
    def test_command_line_overrides_the_file(self, config_file: Path):
        args = build_parser().parse_args(
            ["--config", str(config_file), "--port", "9999", "--interval", "3.0"]
        )
        config = load_effective_config(args)
        assert config.monitor.port == 9999  # CLI wins
        assert config.monitor.collection_interval == 3.0
        assert config.monitor.host == "127.0.0.1"  # from the file
        assert config.database.path.endswith("cli.db")

    def test_invalid_configuration_is_reported_by_main(self, tmp_path: Path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("monitor:\n  port: 999999\n", encoding="utf-8")
        assert main(["--config", str(bad)]) == 2
        assert "configuration error" in capsys.readouterr().err


class TestModes:
    def test_check_config_prints_a_summary(self, config_file: Path, capsys):
        assert main(["--config", str(config_file), "--check-config"]) == 0
        output = capsys.readouterr().out
        assert "Configuration OK" in output
        assert "8123" in output
        assert "download 10" in output

    def test_check_config_rejects_unknown_options(self, tmp_path: Path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("detection:\n  download_threshold: 5\n", encoding="utf-8")
        assert main(["--config", str(bad), "--check-config"]) == 2
        assert "configuration error" in capsys.readouterr().err

    @pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX path form in the fixture")
    def test_once_runs_a_real_cycle_and_reports(self, config_file: Path, capsys):
        """End-to-end CLI smoke test: real collectors, temp database, exit 0."""
        exit_code = main(["--config", str(config_file), "--once"])
        output = capsys.readouterr().out
        assert exit_code == 0
        assert "Local Network Monitor" in output
        assert "Status:" in output
        assert "Traffic" in output
        assert "Connections" in output
        assert "Collectors" in output


class TestBanner:
    def test_banner_contains_the_operational_facts(self, config_file: Path, capsys):
        args = build_parser().parse_args(["--config", str(config_file)])
        config = load_effective_config(args)
        print_banner(config)
        output = capsys.readouterr().out
        for expected in (
            f"v{__version__}",
            "RUNNING",
            "http://127.0.0.1:8123",
            "/api/docs",
            str(config_file),
        ):
            assert expected in output
