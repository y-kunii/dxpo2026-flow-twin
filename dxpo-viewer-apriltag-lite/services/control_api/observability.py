"""Privacy-preserving JSON logging for the local control plane."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


REQUEST_ID = contextvars.ContextVar("spatial_request_id", default="")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RESOURCE_PATTERNS = {
    "job_id": re.compile(r"^/api/jobs/([^/]+)"),
    "project_id": re.compile(r"^/api/project-workspaces/([^/]+)"),
}
_LOG_FIELDS = (
    "event",
    "request_id",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "job_id",
    "project_id",
    "scene_id",
    "scene_package_id",
    "enabled_features",
    "error_type",
)


def accepted_request_id(value: str | None) -> str:
    """Return a safe caller ID or a generated correlation ID."""

    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return uuid.uuid4().hex


def resource_fields(path: str) -> dict[str, str]:
    """Extract non-secret correlation identifiers from the route path."""

    result: dict[str, str] = {}
    for name, pattern in _RESOURCE_PATTERNS.items():
        match = pattern.match(path)
        if match:
            result[name] = match.group(1)
    return result


class JsonFormatter(logging.Formatter):
    """Emit one machine-readable JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        current_request_id = REQUEST_ID.get()
        if current_request_id:
            payload["request_id"] = current_request_id
        for field in _LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None and value != "":
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_json_logging(level: str = "INFO") -> None:
    """Configure application and Uvicorn loggers without duplicate handlers."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"event": event, **fields})


class RequestMetrics:
    """Low-cardinality process metrics with no external runtime dependency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str], float] = defaultdict(float)
        self._duration_count: dict[tuple[str, str], int] = defaultdict(int)

    def begin(self) -> None:
        with self._lock:
            self._in_flight += 1

    def end(self, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        key = (method, route)
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._requests[(method, route, status_code)] += 1
            self._duration_sum[key] += max(0.0, duration_seconds)
            self._duration_count[key] += 1

    @staticmethod
    def _label(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def prometheus_text(self) -> str:
        with self._lock:
            in_flight = self._in_flight
            requests = dict(self._requests)
            duration_sum = dict(self._duration_sum)
            duration_count = dict(self._duration_count)
        lines = [
            "# HELP spatial_http_requests_in_flight Requests currently executing.",
            "# TYPE spatial_http_requests_in_flight gauge",
            f"spatial_http_requests_in_flight {in_flight}",
            "# HELP spatial_http_requests_total Completed HTTP requests.",
            "# TYPE spatial_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(requests.items()):
            lines.append(
                'spatial_http_requests_total{method="%s",route="%s",status="%s"} %d'
                % (self._label(method), self._label(route), status, count)
            )
        lines.extend([
            "# HELP spatial_http_request_duration_seconds Request duration by route template.",
            "# TYPE spatial_http_request_duration_seconds summary",
        ])
        for (method, route), total in sorted(duration_sum.items()):
            labels = 'method="%s",route="%s"' % (self._label(method), self._label(route))
            lines.append(
                f"spatial_http_request_duration_seconds_sum{{{labels}}} {total:.9f}"
            )
            lines.append(
                f"spatial_http_request_duration_seconds_count{{{labels}}} {duration_count[(method, route)]}"
            )
        return "\n".join(lines) + "\n"
