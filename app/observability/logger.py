# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Structured JSON logging for the Study Buddy agent.

Every log record is emitted as a single JSON object (via ``python-json-logger``)
containing rich metadata: severity, logger, trace/span ids (when an
OpenTelemetry span is active), and any ``extra={...}`` fields. Extra fields are
passed through the PII scrubber before emission.

When ``BUDDY_ENABLE_CLOUD_LOGGING=true`` the records are also shipped to Google
Cloud Logging as structured ``jsonPayload`` entries.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger import json as jsonlogger

_CONFIGURED = False

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {"message", "asctime"}


class _ContextEnricher(logging.Filter):
    """Attach OpenTelemetry trace context and scrub PII from extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx and ctx.is_valid:
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id = format(ctx.span_id, "016x")
        except Exception:  # noqa: BLE001
            pass

        from app.observability.pii_redactor import get_pii_scrubber

        scrubber = get_pii_scrubber()
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, (str, dict, list)):
                setattr(record, key, scrubber.redact(value))
        if isinstance(record.msg, str):
            record.msg = scrubber.redact_text(record.msg)
        return True


def configure_logging(level: str | None = None) -> None:
    """Configure root logging once with a JSON formatter."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    from app.config.settings import get_settings

    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "severity", "asctime": "timestamp", "name": "logger"},
        )
    )
    handler.addFilter(_ContextEnricher())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or settings.log_level)

    if settings.enable_cloud_logging and settings.project_id:
        try:
            import google.cloud.logging

            client = google.cloud.logging.Client(project=settings.project_id)
            cloud_handler = client.get_default_handler()
            cloud_handler.addFilter(_ContextEnricher())
            root.addHandler(cloud_handler)
        except Exception:  # noqa: BLE001
            root.warning("cloud_logging_unavailable")

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring JSON logging on first use."""
    configure_logging()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Convenience helper to emit a structured event with arbitrary fields.

    Field names that collide with reserved ``LogRecord`` attributes (e.g.
    ``args``, ``msg``) are prefixed with ``field_`` so logging never raises.
    """
    safe = {(f"field_{k}" if k in _RESERVED else k): v for k, v in fields.items()}
    logger.log(level, event, extra={"event": event, **safe})
