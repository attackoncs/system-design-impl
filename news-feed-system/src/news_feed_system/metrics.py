"""Thread-safe, bounded observations for the two public feed operations."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Iterator

_WINDOW_SECONDS = 60


@dataclass
class _Counters:
    requests: int = 0
    in_flight: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    buckets: list[tuple[int, int]] = field(
        default_factory=lambda: [(-1, 0)] * _WINDOW_SECONDS
    )


class RequestMetrics:
    """Observe acceptance/read latency without retaining individual requests.

    Only ``publish`` and ``retrieve`` are valid labels. Each uses 60 one-second
    buckets for recent request starts, plus lifetime outcome/latency counters.
    QPS divides the count in the current and preceding 59 buckets by 60, even
    during startup. It is a bucketed estimate, not an exact sliding window.
    Locks protect counters only; the measured operation runs outside the lock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._operations = {name: _Counters() for name in ("publish", "retrieve")}

    @contextmanager
    def measure(self, operation: str) -> Iterator[None]:
        with self._lock:
            if operation not in self._operations:
                raise ValueError("Only publish and retrieve operations are supported")
            stats = self._operations[operation]
            started = self._clock()
            second = int(started)
            index = second % _WINDOW_SECONDS
            previous_second, count = stats.buckets[index]
            stats.buckets[index] = (
                second,
                count + 1 if previous_second == second else 1,
            )
            stats.requests += 1
            stats.in_flight += 1
        outcome = "succeeded"
        try:
            yield
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            with self._lock:
                elapsed = max(0.0, self._clock() - started)
                stats.in_flight -= 1
                setattr(stats, outcome, getattr(stats, outcome) + 1)
                stats.total_seconds += elapsed
                stats.max_seconds = max(stats.max_seconds, elapsed)

    def snapshot(self) -> dict[str, dict]:
        """Return independent JSON-compatible aggregate values for monitoring."""
        with self._lock:
            second = int(self._clock())
            result = {}
            for operation, stats in self._operations.items():
                recent = sum(
                    count
                    for bucket, count in stats.buckets
                    if 0 <= second - bucket < _WINDOW_SECONDS
                )
                completed = stats.succeeded + stats.failed + stats.cancelled
                result[operation] = {
                    "requests": stats.requests,
                    "in_flight": stats.in_flight,
                    "succeeded": stats.succeeded,
                    "failed": stats.failed,
                    "cancelled": stats.cancelled,
                    "requests_last_60s": recent,
                    "qps_last_60s": recent / _WINDOW_SECONDS,
                    "latency_seconds": {
                        "count": completed,
                        "total": stats.total_seconds,
                        "mean": stats.total_seconds / completed if completed else 0.0,
                        "max": stats.max_seconds,
                    },
                }
            return result
