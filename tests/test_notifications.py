"""Notification tests: policy, rendering and SMTP behaviour.

The rule that matters here is the one from the specification: sending email must
not block the monitoring loop. Delivery is therefore tested through the manager
(threaded, bounded, cooldown-aware) and the notifier's failure paths are tested
so an unreachable SMTP server is a logged non-event, never an exception that
reaches the loop.
"""

from __future__ import annotations

import smtplib
from datetime import timedelta

import pytest

from network_monitor.config import EmailSection, NotificationsSection
from network_monitor.models.events import Event, EventSeverity, EventType
from network_monitor.models.network import utc_now
from network_monitor.notifications import EmailNotifier, NotificationManager


def make_event(
    event_type: EventType = EventType.HIGH_DOWNLOAD,
    severity: EventSeverity = EventSeverity.WARNING,
    **overrides,
) -> Event:
    payload = dict(
        timestamp=utc_now(),
        event_type=event_type,
        severity=severity,
        title="High download traffic on Ethernet",
        description="Download traffic reached 20.0 Mb/s, above the configured threshold.",
        interface_name="Ethernet",
        evidence={"interface": "Ethernet", "download_rate_mbps": 20.0, "threshold_mbps": 10.0},
    )
    payload.update(overrides)
    return Event(**payload)


class TestEmailRendering:
    def test_subject_carries_severity_and_title(self):
        subject = EmailNotifier.subject_for(make_event())
        assert subject == "[Local Network Monitor] WARNING: High download traffic on Ethernet"

    def test_body_answers_the_five_questions(self):
        body = EmailNotifier.body_for(make_event(pid=8420, process_name="chrome.exe"))
        assert "What:" in body and "High download traffic" in body
        assert "When:" in body
        assert "Interface: Ethernet" in body
        assert "Severity:  WARNING" in body
        assert "Why it was detected:" in body
        assert "Evidence:" in body
        assert "download_rate_mbps: 20.0" in body

    def test_body_includes_the_executable_context_when_present(self):
        body = EmailNotifier.body_for(make_event(pid=99, process_name="svchost.exe"))
        assert "svchost.exe (PID 99)" in body

    def test_body_handles_events_without_evidence(self):
        body = EmailNotifier.body_for(make_event(evidence={}))
        assert "no structured evidence recorded" in body

    def test_message_headers_are_ready_to_send(self):
        notifier = EmailNotifier(
            EmailSection(
                enabled=True,
                smtp_host="smtp.example.com",
                sender="monitor@example.com",
                recipients=["ops@example.com", "oncall@example.com"],
            )
        )
        message = notifier.build_message(make_event())
        assert message["To"] == "ops@example.com, oncall@example.com"
        assert message["X-LNM-Severity"] == "warning"


class TestEmailDelivery:
    class FakeSMTP:
        """Records the SMTP conversation without touching the network."""

        instances: list = []

        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.calls: list = []
            self.__class__.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def ehlo(self):
            self.calls.append("ehlo")

        def starttls(self):
            self.calls.append("starttls")

        def login(self, username, password):
            self.calls.append(("login", username, password))

        def send_message(self, message):
            self.calls.append(("send", message["Subject"]))

    @pytest.fixture()
    def notifier(self) -> EmailNotifier:
        self.FakeSMTP.instances = []
        return EmailNotifier(
            EmailSection(
                enabled=True,
                smtp_host="smtp.example.com",
                smtp_port=587,
                use_tls=True,
                username="monitor",
                password="secret",
                sender="monitor@example.com",
                recipients=["ops@example.com"],
            )
        )

    def test_successful_delivery_uses_tls_and_login(self, notifier, monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", self.FakeSMTP)
        assert notifier.send(make_event()) is True
        smtp = self.FakeSMTP.instances[0]
        assert "starttls" in smtp.calls
        assert ("login", "monitor", "secret") in smtp.calls
        assert any(call[0] == "send" for call in smtp.calls if isinstance(call, tuple))

    def test_plain_smtp_skips_starttls(self, notifier, monkeypatch):
        notifier.config.use_tls = False
        monkeypatch.setattr(smtplib, "SMTP", self.FakeSMTP)
        assert notifier.send(make_event()) is True
        assert "starttls" not in self.FakeSMTP.instances[0].calls

    def test_smtp_failure_returns_false_instead_of_raising(self, notifier, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise smtplib.SMTPConnectError(421, "service not available")

        monkeypatch.setattr(smtplib, "SMTP", refuse)
        assert notifier.send(make_event()) is False

    def test_network_error_returns_false(self, notifier, monkeypatch):
        def unreachable(*_args, **_kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(smtplib, "SMTP", unreachable)
        assert notifier.send(make_event()) is False

    def test_disabled_notifier_does_not_attempt_delivery(self, monkeypatch):
        sent = []
        monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: sent.append(1))
        notifier = EmailNotifier(EmailSection(enabled=False))
        assert notifier.send(make_event()) is False
        assert sent == []

    def test_password_comes_from_the_environment_when_set(self, notifier, monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", self.FakeSMTP)
        monkeypatch.setenv("LNM_SMTP_PASSWORD", "from-env")
        notifier.send(make_event())
        assert ("login", "monitor", "from-env") in self.FakeSMTP.instances[0].calls


class TestNotificationPolicy:
    def test_disabled_manager_ignores_events(self):
        manager = NotificationManager(NotificationsSection(enabled=False), notifiers=[])
        assert manager.handle_events([make_event(severity=EventSeverity.CRITICAL)]) == 0

    def test_severity_floor_filters_low_severity_events(self):
        config = NotificationsSection(enabled=True, min_severity="warning")
        manager = NotificationManager(config, notifiers=[])
        info = make_event(EventType.NEW_NETWORK_PROCESS, EventSeverity.INFO)
        warning = make_event(EventType.HIGH_UPLOAD, EventSeverity.WARNING)
        assert manager.handle_events([info, warning]) == 1

    def test_cooldown_collapses_repeated_events(self):
        config = NotificationsSection(enabled=True, cooldown_seconds=300)
        manager = NotificationManager(config, notifiers=[])
        first = make_event()
        second = make_event(timestamp=utc_now() + timedelta(seconds=30))
        assert manager.handle_events([first]) == 1
        assert manager.handle_events([second]) == 0
        assert manager.suppressed == 1

    def test_manager_reports_its_own_state(self):
        manager = NotificationManager(NotificationsSection(enabled=True), notifiers=[])
        status = manager.status()
        assert status["enabled"] is True
        assert status["queued"] == 0
        assert status["worker_running"] is False  # nothing started it yet

    def test_stop_is_safe_without_start(self):
        NotificationManager(NotificationsSection(enabled=True), notifiers=[]).stop()
