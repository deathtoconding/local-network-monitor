"""Notification layer: policy (manager) separated from transport (notifiers)."""

from .email import EmailNotifier
from .manager import NotificationManager, NotificationRecord, Notifier

__all__ = [
    "EmailNotifier",
    "NotificationManager",
    "NotificationRecord",
    "Notifier",
]
