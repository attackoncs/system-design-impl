"""Strict file loading and an optional, explicitly owned polling worker."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from pathlib import Path

from rate_limiter.config import Algorithm, RateLimitRule


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def read_rules(path) -> list[RateLimitRule]:
    with Path(path).open("rb") as stream:
        payload = stream.read(65_537)
    if len(payload) > 65_536:
        raise ValueError("rule file exceeds 64 KiB")
    return parse_rules(payload)


def parse_rules(payload: bytes) -> list[RateLimitRule]:
    data = json.loads(payload, object_pairs_hook=_unique_fields)
    if not isinstance(data, dict) or set(data) != {"rules"}:
        raise ValueError("rule file must contain only rules")
    if not isinstance(data["rules"], list) or len(data["rules"]) > 100:
        raise ValueError("rules must be a list of at most 100 entries")
    rules = []
    fields = {
        "limit",
        "window",
        "algorithm",
        "key_type",
        "path_pattern",
        "name",
        "capacity",
    }
    for item in data["rules"]:
        if (
            not isinstance(item, dict)
            or not {"limit", "window"} <= item.keys()
            or item.keys() - fields
        ):
            raise ValueError("invalid rule fields")
        arguments = dict(item)
        arguments["algorithm"] = Algorithm(arguments.get("algorithm", "token_bucket"))
        rules.append(RateLimitRule(**arguments))
    return rules


class RuleReloader:
    """Publish valid complete files; retain the previous rules on any error.

    Publish files with os.replace. The owner must close this worker before
    closing shared storage. Counters and errors never include request subjects.
    """

    def __init__(self, limiter, path, interval: float = 1.0):
        if not math.isfinite(interval) or interval < 0.05:
            raise ValueError("interval must be finite and >= 0.05 seconds")
        self.limiter, self.path, self.interval = limiter, Path(path), interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._digest = None
        self._status = {"reloads": 0, "errors": 0, "last_error": None}

    def poll(self) -> bool:
        with self._lock:
            try:
                # Parse exactly the bounded bytes associated with this digest.
                with self.path.open("rb") as stream:
                    payload = stream.read(65_537)
                if len(payload) > 65_536:
                    raise ValueError("rule file exceeds 64 KiB")
                digest = hashlib.sha256(payload).digest()
                if digest == self._digest:
                    return False
                self.limiter.update_rules(parse_rules(payload))
                self._digest = digest
                self._status["reloads"] += 1
                self._status["last_error"] = None
                return True
            except (OSError, ValueError, TypeError) as error:
                self._status["errors"] += 1
                message = type(error).__name__
                if self._status["last_error"] != message:
                    logging.getLogger(__name__).warning(
                        "Rule reload failed: %s", message
                    )
                self._status["last_error"] = message
                return False

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("reloader already started")
        self.poll()
        self._thread = threading.Thread(
            target=self._run, name="rate-limit-rules", daemon=False
        )
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.interval):
            self.poll()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()
