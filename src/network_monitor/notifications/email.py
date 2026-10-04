"""Email notifier.

Sends one plain-text email per event through SMTP. Kept intentionally boring:
the MVP needs a notification channel, not a templating engine.

Never raises: :class:`Notifications.manager.NotificationManager` expects
``send`` to return a boolean so the worker thread survives any SMTP problem.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional

from ..config import EmailSection
from ..models.events import Event
from .manager import Notifier

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 15


class EmailNotifier(Notifier):
    """Delivers events as plain-text emails."""

    name = "email"

    def __init__(self, config: EmailSection) -> None:
        self.config = config

    # ---- rendering ---------------------------------------------------
    @staticmethod
    def subject_for(event: Event) -> str:
        return f"[Local Network Monitor] {event.severity.value.upper()}: {event.title}"

    @staticmethod
    def body_for(event: Event) -> str:
        """Human-readable body: what, when, where, how severe, why, evidence."""
        lines = [
            "Local Network Monitor event",
            "=" * 40,
            "",
            f"What:      {event.title}",
            f"Type:      {event.event_type.value}",
            f"Severity:  {event.severity.value.upper()}",
            f"When:      {event.timestamp.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        ]
        if event.interface_name:
            lines.append(f"Interface: {event.interface_name}")
        if event.pid is not None:
            lines.append(f"Process:   {event.process_name or 'unknown'} (PID {event.pid})")
        lines += [
            f"Source:    {event.source}",
            "",
            "Why it was detected:",
            f"  {event.description}",
            "",
            "Evidence:",
        ]
        if event.evidence:
            for key, value in sorted(event.evidence.items()):
                lines.append(f"  {key}: {value}")
        else:
            lines.append("  (no structured evidence recorded)")
        lines += ["", f"Event id: {event.id if event.id is not None else 'not stored'}", ""]
        return "\n".join(lines)

    def build_message(self, event: Event) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = self.subject_for(event)
        message["From"] = self.config.sender
        message["To"] = ", ".join(self.config.recipients)
        message["X-LNM-Event-Type"] = event.event_type.value
        message["X-LNM-Severity"] = event.severity.value
        message.set_content(self.body_for(event))
        return message

    # ---- delivery ----------------------------------------------------
    def send(self, event: Event) -> bool:
        """Send an email for the event. Returns False (never raises) on failure."""
        if not self.config.is_usable():
            logger.debug("email notifier not usable; skipping event %s", event.id)
            return False

        message = self.build_message(event)
        password: Optional[str] = self.config.resolved_password() or None

        try:
            with smtplib.SMTP(
                self.config.smtp_host, self.config.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.ehlo()
                if self.config.use_tls:
                    smtp.starttls()
                    smtp.ehlo()
                if self.config.username:
                    smtp.login(self.config.username, password or "")
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            logger.error("email delivery failed for event %s: %s", event.id, exc)
            return False

        logger.info(
            "email notification sent for %s event to %s",
            event.event_type.value,
            ", ".join(self.config.recipients),
        )
        return True
