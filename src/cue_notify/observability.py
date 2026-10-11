"""Logging setup: human-friendly console output in development, JSON lines in production.

Modules log through the standard library (``logging.getLogger(__name__)``) and attach
structured context with ``extra={...}``; the JSON formatter emits those fields verbatim.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Literal

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


# Paths that carry a credential: preference pages, review links, open tracking.
_TOKEN_PATH = re.compile(r"/(preferences|review|track)/[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    return _TOKEN_PATH.sub(r"/\1/[redacted]", text)


class RedactTokens(logging.Filter):
    """Keeps tokens in request paths (access log, error context) out of the logs; anyone
    who can read the logs could otherwise change preferences or decide reviews."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/" in message:
            record.msg, record.args = redact(message), None
        for key, value in _extra(record).items():
            if isinstance(value, str) and "/" in value:
                setattr(record, key, redact(value))
        return True


def _extra(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in vars(record).items() if key not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **_extra(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = _extra(record)
        if extra:
            line += " " + " ".join(f"{key}={value}" for key, value in extra.items())
        return line


def configure_logging(level: str = "INFO", fmt: Literal["console", "json"] = "console") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(RedactTokens())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn installs its own handlers; route everything through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    for noisy in ("httpx", "httpcore", "aiosqlite", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


class Metrics:
    """Prometheus metrics, or a no-op when disabled or ``prometheus-client`` is missing."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = False
        self._event_names: set[str] = set()
        if not enabled:
            return
        try:
            from prometheus_client import (
                CollectorRegistry,
                Counter,
                Histogram,
            )
        except ImportError:
            logging.getLogger(__name__).warning(
                "metrics enabled but prometheus-client is missing: "
                "pip install 'cue-notify[metrics]'"
            )
            return
        self.enabled = True
        self.registry = CollectorRegistry()
        self._events = Counter(
            "cue_events_total", "Events ingested", ["name"], registry=self.registry
        )
        self._messages = Counter(
            "cue_messages_total",
            "Messages reaching a final status",
            ["status", "channel", "category"],
            registry=self.registry,
        )
        self._jobs = Histogram(
            "cue_job_duration_seconds",
            "Background job duration",
            ["kind", "outcome"],
            registry=self.registry,
        )

    MAX_EVENT_NAMES = 200
    """Distinct event-name labels; any producer can invent names, so later ones are
    counted as ``other`` instead of growing memory without bound."""

    def event_ingested(self, name: str) -> None:
        if not self.enabled:
            return
        if name not in self._event_names:
            if len(self._event_names) >= self.MAX_EVENT_NAMES:
                name = "other"
            else:
                self._event_names.add(name)
        self._events.labels(name=name).inc()

    def message_finished(self, status: str, channel: str | None, category: str) -> None:
        if self.enabled:
            self._messages.labels(status=status, channel=channel or "", category=category).inc()

    def job_finished(self, kind: str, outcome: str, seconds: float) -> None:
        if self.enabled:
            self._jobs.labels(kind=kind, outcome=outcome).observe(seconds)

    def render(self) -> tuple[bytes, str]:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return generate_latest(self.registry), CONTENT_TYPE_LATEST
