"""Uvicorn websocket handshake logs bypass --no-access-log; redact ephemeral URL credentials."""

import logging
import re

SENSITIVE_QUERY = re.compile(r"(?i)\b(ticket|code|state)=([^&\s\"']+)")


def redact(value):
    if isinstance(value, str):
        return SENSITIVE_QUERY.sub(r"\1=[REDACTED]", value)
    if isinstance(value, tuple):
        return tuple(redact(v) for v in value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


class SensitiveQueryFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.msg)
        record.args = redact(record.args)
        return True


class ApplicationLogHandler(logging.StreamHandler):
    """Marker handler so repeated app construction cannot duplicate log lines."""


def configure_logging():
    for name in ("uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, SensitiveQueryFilter) for f in logger.filters):
            logger.addFilter(SensitiveQueryFilter())

    application_logger = logging.getLogger("app")
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    if not any(
        isinstance(handler, ApplicationLogHandler)
        for handler in application_logger.handlers
    ):
        handler = ApplicationLogHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler.addFilter(SensitiveQueryFilter())
        application_logger.addHandler(handler)
