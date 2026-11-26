"""Контекст запроса и журнал без учётных данных и содержимого базы."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import time


logger = logging.getLogger("onec_data")


@dataclass
class RequestTrace:
    request_id: str
    deadline: float
    started: float = field(default_factory=time.monotonic)
    metrics: dict = field(default_factory=lambda: {"upstream_requests": 0, "upstream_retries": 0, "upstream_ms": 0.0})


current_trace = ContextVar("onec_request_trace", default=None)


@contextmanager
def request_scope(request_id, timeout):
    parent = current_trace.get()
    deadline = time.monotonic() + timeout
    trace = RequestTrace(request_id, min(deadline, parent.deadline) if parent else deadline)
    if parent:
        trace.started, trace.metrics = parent.started, parent.metrics
    token = current_trace.set(trace)
    try:
        yield trace
    finally:
        current_trace.reset(token)


def log_event(event, **fields):
    trace = current_trace.get()
    logger.info(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
                           "request_id": trace.request_id if trace else None, **fields}, ensure_ascii=False))


def configure_logging():
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
