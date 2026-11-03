"""Notifier interface and implementations.

Add Pushover/Discord later by implementing `Notifier.send`; detection and
state logic only ever see this interface.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import requests

from state import Notification

log = logging.getLogger(__name__)

_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class Notifier(Protocol):
    def send(self, note: Notification) -> bool:
        """Deliver a notification. Returns True on success; must not raise."""
        ...


class NtfyNotifier:
    """Publishes via ntfy's JSON API (POST to the server root), which keeps
    UTF-8 titles intact, unlike header-based publishing."""

    def __init__(
        self,
        server: str,
        topic: str,
        token: str | None = None,
        session: requests.Session | None = None,
        attempts: int = 3,
        timeout: float = 10,
        sleep=time.sleep,
    ) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.session = session or requests.Session()
        self.attempts = attempts
        self.timeout = timeout
        self.sleep = sleep

    def send(self, note: Notification) -> bool:
        payload: dict = {
            "topic": self.topic,
            "title": note.title,
            "message": note.message,
            "priority": _NTFY_PRIORITY.get(note.priority, 3),
        }
        if note.tags:
            payload["tags"] = list(note.tags)
        if note.click_url:
            payload["click"] = note.click_url
            payload["actions"] = [{"action": "view", "label": "Open product", "url": note.click_url}]
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}

        for attempt in range(1, self.attempts + 1):
            try:
                resp = self.session.post(self.server, json=payload, headers=headers, timeout=self.timeout)
                if resp.ok:
                    log.info("notification sent", extra={"fields": {"kind": note.kind, "attempt": attempt}})
                    return True
                error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "notification failed",
                extra={"fields": {"kind": note.kind, "attempt": attempt, "error": error}},
            )
            if attempt < self.attempts:
                self.sleep(2 ** (attempt - 1))
        return False


class LogNotifier:
    """Dry-run notifier: logs instead of sending."""

    def send(self, note: Notification) -> bool:
        log.info(
            "DRY RUN notification",
            extra={"fields": {"kind": note.kind, "title": note.title, "priority": note.priority}},
        )
        return True
