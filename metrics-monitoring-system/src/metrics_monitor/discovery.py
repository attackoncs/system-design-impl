"""Reloadable target discovery and bounded scrapes with static ring ownership.

All collectors must receive the same membership/target configuration. This is a
deterministic placement reference, not consensus discovery or automatic failover.
"""

from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .collector import finite, integer
from .common import identifier, labels

LOGGER = logging.getLogger(__name__)


def _hash(value):
    return int.from_bytes(hashlib.sha256(value.encode()).digest(), "big")


class CollectorRing:
    def __init__(self, collectors, virtual_nodes=64):
        if not isinstance(collectors, (list, tuple)) or not 1 <= len(collectors) <= 64:
            raise ValueError("collector membership requires 1..64 IDs")
        nodes = [identifier(node) for node in collectors]
        if len(nodes) != len(set(nodes)):
            raise ValueError("duplicate collector ID")
        integer(virtual_nodes, 1, 256)
        self.members = tuple(sorted(nodes))
        self.points = tuple(
            sorted(
                (_hash(f"{node}/{index}"), node)
                for node in self.members
                for index in range(virtual_nodes)
            )
        )
        self.hashes = tuple(point[0] for point in self.points)

    def owner(self, instance):
        identifier(instance)
        index = bisect_left(self.hashes, _hash(instance)) % len(self.points)
        return self.points[index][1]


def target_config(target, default_interval):
    if not isinstance(target, dict) or set(target) - {
        "instance",
        "url",
        "token",
        "labels",
        "interval",
        "timeout",
        "retries",
    }:
        raise ValueError("invalid scrape target fields")
    target = json.loads(json.dumps(target, allow_nan=False))
    identifier(target["instance"])
    if target["instance"] == "_discovery":
        raise ValueError("reserved discovery instance")
    url = target["url"]
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or any(ord(char) <= 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("invalid exporter URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.port == 0
    ):
        raise ValueError("invalid exporter URL")
    target["interval"] = finite(target.get("interval", default_interval), 0.2, 86400)
    target["timeout"] = finite(target.get("timeout", 5), 0.05, 30)
    target["retries"] = integer(target.get("retries", 0), 0, 2)
    target["labels"] = labels(target.get("labels", {}))
    labels({**target["labels"], "instance": target["instance"]})
    if (
        "instance" in target["labels"]
        and target["labels"]["instance"] != target["instance"]
    ):
        raise ValueError("discovery instance label conflict")
    if target.get("token") is not None and not isinstance(target["token"], str):
        raise ValueError("invalid exporter token")
    return target


class ScrapeScheduler:
    def __init__(
        self,
        collector,
        targets_path=None,
        *,
        workers=4,
        collector_id="local",
        collectors=("local",),
        default_interval=5,
        clock=time.monotonic,
    ):
        integer(workers, 1, 32)
        finite(default_interval, 0.2, 86400)
        self.ring = CollectorRing(collectors)
        self.collector_id = identifier(collector_id)
        if self.collector_id not in self.ring.members or not callable(clock):
            raise ValueError("collector must be in membership")
        self.collector, self.path = (
            collector,
            Path(targets_path) if targets_path else None,
        )
        self.workers, self.default_interval, self.clock = (
            workers,
            default_interval,
            clock,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="metric-scrape"
        )
        self._lock = threading.Lock()
        self._closed = False
        self._targets, self._due = {}, {}
        self.last_error = None

    def _reload(self):
        if self.path is None:
            return
        with self.path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("target config exceeds 1 MiB")
        body = json.loads(raw)
        if not isinstance(body, list) or len(body) > 1000:
            raise ValueError("target list exceeds 1000 entries")
        targets = {}
        for value in body:
            target = target_config(value, self.default_interval)
            instance = target["instance"]
            if instance in targets:
                raise ValueError("duplicate target instance")
            targets[instance] = target
        owned = {
            key: value
            for key, value in targets.items()
            if self.ring.owner(key) == self.collector_id
        }
        for instance, target in owned.items():
            if self._targets.get(instance) != target:
                self._due[instance] = 0
        self._targets = owned
        self._due = {key: self._due.get(key, 0) for key in owned}

    def run_once(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("scrape scheduler closed")
            previous_error = self.last_error
            errors = []
            try:
                self._reload()
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                errors.append(type(exc).__name__)
                LOGGER.warning(
                    "target configuration invalid; retaining last valid targets"
                )
            now = finite(self.clock(), 0, 1e15)
            selected = sorted(
                (due, key) for key, due in self._due.items() if due <= now
            )[: self.workers]
            futures = []
            for _, instance in selected:
                target = self._targets[instance]
                self._due[instance] = now + target["interval"]
                futures.append(self._executor.submit(self.collector.scrape, target))
            # Drain every accepted operation even when one fails, keeping the
            # executor backlog bounded and preventing a fast poison from starving peers.
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(type(exc).__name__)
                    LOGGER.warning(
                        "target scrape enqueue failed; later scrapes will retry"
                    )
            self.last_error = "scrape discovery or enqueue failed" if errors else None
            if errors or previous_error:
                with self.collector.transaction(True) as db:
                    db.execute(
                        "INSERT INTO scrapes VALUES('_discovery',?,?) ON CONFLICT(target) DO UPDATE SET updated=excluded.updated,error=excluded.error",
                        (self.collector._now(), self.last_error or ""),
                    )
            return len(selected)

    def close(self):
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=True)
