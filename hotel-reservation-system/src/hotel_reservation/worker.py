"""Owned saga worker: unknown payment outcomes retain the durable intent."""

import json
import logging
import math
import threading
import time

from .common import dumps, http_json, integer
from .payment import SimulatedHTTPPayment
from .payment_contracts import normalize_job, normalize_receipt

LOGGER = logging.getLogger(__name__)


def _duration(value, maximum=3600):
    if (
        type(value) not in (int, float)
        or not 0 < value <= maximum
        or not math.isfinite(value)
    ):
        raise ValueError("duration must be finite and positive")
    return value


class Worker:
    def __init__(self, config, shard, *, provider=None, interval=0.05, timeout=10):
        owned = json.loads(dumps(config))
        urls = owned.get("shard_urls")
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 32
            or any(not isinstance(url, str) for url in urls)
        ):
            raise ValueError("shard URL list required")
        integer(shard, "shard", 0, len(urls) - 1)
        self.url, self.token = urls[shard].rstrip("/"), owned["internal_token"]
        self.provider = (
            provider if provider is not None else SimulatedHTTPPayment(owned)
        )
        if not callable(getattr(self.provider, "execute", None)):
            raise ValueError("payment adapter required")
        self.interval, self.timeout = _duration(interval), _duration(timeout)
        self.stop = threading.Event()
        self.last_error = None
        self.processed = 0
        self._lifecycle = threading.Lock()
        self._state = threading.Condition()
        self._operation = threading.Lock()
        self._local = threading.local()
        self._active, self._closed = 0, False
        self._thread = threading.Thread(target=self.run, name=f"hotel-saga-{shard}")
        self._started = False

    def _call(self, path, body, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or self.stop.is_set():
            raise TimeoutError("worker operation expired")
        return http_json(
            self.url + path,
            self.token,
            body,
            timeout=min(5, remaining),
            deadline=deadline,
            cancel=self.stop,
        )

    def run_once(self):
        with self._state:
            if self._closed:
                raise RuntimeError("worker is closed")
            self._active += 1
            self._local.active = True
        try:
            with self._operation:
                if self.stop.is_set():
                    return False
                return self._work()
        finally:
            with self._state:
                self._active -= 1
                self._local.active = False
                self._state.notify_all()

    def _work(self):
        deadline, job = time.monotonic() + self.timeout, None
        try:
            sweep = self._call("/internal/sweep", {}, deadline)
            integer(sweep["expired"], "expired holds", 0, 100)
            response = self._call("/internal/claim", {}, deadline)
            if not isinstance(response, dict) or set(response) != {"job"}:
                raise ValueError("invalid claim response")
            if response["job"] is None:
                self.last_error = None
                return False
            job = normalize_job(response["job"])
            execute = getattr(self.provider, "execute_with_budget", None)
            if callable(execute):
                result = execute(dict(job), deadline=deadline, cancel=self.stop)
            else:
                # Legacy/custom adapters retain their execute(job) contract. Their
                # blocking work cannot be forcefully cancelled; close drains it.
                result = self.provider.execute(dict(job))
            result = normalize_receipt(result, job)
            settled = self._call(
                "/internal/finish", {"job": job, "result": result}, deadline
            )
            if (
                not isinstance(settled, dict)
                or set(settled) != {"settled"}
                or type(settled["settled"]) is not bool
            ):
                raise ValueError("invalid settlement confirmation")
            self.last_error = None
            self.processed += int(settled["settled"])
            return True
        except Exception:
            self.last_error = "payment work failed; durable intent will retry"
            LOGGER.warning(self.last_error)
            if job is not None and not self.stop.is_set():
                try:
                    # A timeout or malformed receipt is never a payment decline.
                    # If this callback also fails, the persisted lease expires.
                    self._call(
                        "/internal/retry",
                        {
                            "job": job,
                            "error": "payment or settlement confirmation unavailable",
                        },
                        deadline,
                    )
                except Exception:
                    self.last_error = (
                        "payment retry callback unavailable; lease recovery required"
                    )
                    LOGGER.warning(self.last_error)
            return False

    def run(self):
        while not self.stop.is_set():
            try:
                worked = self.run_once()
            except RuntimeError:
                if self.stop.is_set():
                    break
                raise
            if not worked:
                self.stop.wait(0.1 if self.last_error else self.interval)

    def start(self):
        with self._lifecycle:
            if self._closed or self.stop.is_set():
                raise RuntimeError("worker is closed")
            if self._started:
                raise RuntimeError("worker already started")
            self._thread.start()
            self._started = True
        return self

    def close(self):
        if (
            getattr(self._local, "active", False)
            or self._thread.ident == threading.get_ident()
        ):
            raise RuntimeError("cannot synchronously close from worker operation")
        with self._lifecycle:
            with self._state:
                self._closed = True
                self.stop.set()
            started = self._started
        if started:
            self._thread.join()
        with self._state:
            self._state.wait_for(lambda: self._active == 0)


def run(config, shard):
    """Compatibility blocking entry point, with interruption and complete drain."""
    worker = Worker(config, shard)
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
