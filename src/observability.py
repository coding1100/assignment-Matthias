"""Logging, tracing, and in-memory metrics for the analytics pipeline."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


def configure_logging(level: str = "INFO") -> None:
    """Configure process-wide logging exactly once."""
    logger = logging.getLogger("analytics_pipeline")
    if logger.handlers:
        logger.setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def _json_log(level: str, event: str, **fields: Any) -> None:
    payload = {"level": level, "event": event, **fields}
    logger = logging.getLogger("analytics_pipeline")
    message = json.dumps(payload, default=str, ensure_ascii=True)
    if level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
    elif level == "DEBUG":
        logger.debug(message)
    else:
        logger.info(message)


class MetricsRegistry:
    """Thread-safe in-memory metrics registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latency_samples: dict[str, list[float]] = defaultdict(list)

    def inc(self, key: str, value: int = 1) -> None:
        with self._lock:
            self._counters[key] += value

    def observe_latency(self, stage: str, duration_ms: float) -> None:
        with self._lock:
            self._latency_samples[stage].append(float(max(0.0, duration_ms)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
            latencies = {
                stage: {
                    "count": len(samples),
                    "avg_ms": (sum(samples) / len(samples)) if samples else 0.0,
                    "max_ms": max(samples) if samples else 0.0,
                }
                for stage, samples in self._latency_samples.items()
            }
        return {"counters": counters, "latencies": latencies}


GLOBAL_METRICS = MetricsRegistry()


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    trace_id: str

    @classmethod
    def create(cls, request_id: str | None = None) -> "RequestContext":
        rid = (request_id or "").strip() or str(uuid.uuid4())
        return cls(request_id=rid, trace_id=str(uuid.uuid4()))


@contextmanager
def traced_stage(ctx: RequestContext, stage: str) -> Iterator[float]:
    """Context manager that emits stage-level trace/log/metric data."""
    start = time.perf_counter()
    _json_log(
        "DEBUG",
        "stage_start",
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        stage=stage,
    )
    try:
        yield start
        duration_ms = (time.perf_counter() - start) * 1000
        GLOBAL_METRICS.observe_latency(stage, duration_ms)
        _json_log(
            "INFO",
            "stage_complete",
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            stage=stage,
            duration_ms=round(duration_ms, 3),
        )
    except Exception as exc:
        duration_ms = (time.perf_counter() - start) * 1000
        GLOBAL_METRICS.observe_latency(stage, duration_ms)
        GLOBAL_METRICS.inc("stage_errors_total", 1)
        _json_log(
            "ERROR",
            "stage_failed",
            request_id=ctx.request_id,
            trace_id=ctx.trace_id,
            stage=stage,
            duration_ms=round(duration_ms, 3),
            error=str(exc),
        )
        raise


def log_request_start(ctx: RequestContext, question: str) -> None:
    GLOBAL_METRICS.inc("requests_total", 1)
    _json_log(
        "INFO",
        "request_start",
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        question=question,
    )


def log_request_end(ctx: RequestContext, status: str, total_ms: float, total_tokens: int) -> None:
    GLOBAL_METRICS.inc(f"requests_status_{status}", 1)
    GLOBAL_METRICS.observe_latency("pipeline_total", total_ms)
    _json_log(
        "INFO",
        "request_end",
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        status=status,
        total_ms=round(total_ms, 3),
        total_tokens=int(max(0, total_tokens)),
    )
