"""Authorized current-state reads with bounded, optional Redis acceleration.

SQLite remains the privacy/ordering authority. Redis values and Pub/Sub messages
are disposable hints; a failed repair must not turn into one timeout per friend.
"""

from collections import OrderedDict
from contextlib import contextmanager, suppress
import math
import re
import threading
import time

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from .core import Error, Ring, dumps, meters, redis_url, user_id

_WARM = """
local old=redis.pcall('GET',KEYS[1])
if old then
  local ok,value=pcall(cjson.decode,old)
  if ok and type(value)=='table' and type(value.revision)=='number'
     and value.revision>tonumber(ARGV[1]) then return 0 end
end
redis.call('SET',KEYS[1],ARGV[2],'PX',ARGV[3]); return 1
"""


class Backend:
    """Own finite Redis pools and drain accepted work before closing them.

    Socket timeouts apply to individual Redis calls. ``repair_budget`` stops
    optional batches between calls; it cannot interrupt system DNS, SQLite or
    an already-running socket call. No retries are hidden inside the client.
    """

    def __init__(
        self,
        store,
        cache_url,
        namespace="nearby",
        *,
        broker_capacity=32,
        repair_budget=1,
    ):
        if not isinstance(namespace, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", namespace
        ):
            raise ValueError("invalid namespace")
        if type(broker_capacity) is not int or not 1 <= broker_capacity <= 32:
            raise ValueError("broker capacity must be 1..32")
        try:
            valid_budget = (
                type(repair_budget) in (int, float)
                and math.isfinite(repair_budget)
                and 0 < repair_budget <= 10
            )
        except OverflowError:
            valid_budget = False
        if not valid_budget:
            raise ValueError("repair budget must be finite and in (0,10]")
        redis_url(cache_url)
        self.store, self.namespace = store, namespace
        self.broker_capacity, self.repair_budget = broker_capacity, repair_budget
        self.cache = self._client(cache_url, decode=True)
        self.brokers = OrderedDict()
        self._broker_users = {}
        self._condition = threading.Condition()
        self._local = threading.local()
        self._active = 0
        self._closing = self._closed = False
        self._ring = None

    @staticmethod
    def _client(url, *, decode=False):
        redis_url(url)  # URL query options must not override resource bounds.
        return redis.Redis.from_url(
            url,
            decode_responses=decode,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            max_connections=16,
            retry=Retry(NoBackoff(), 0),
        )

    @contextmanager
    def _operation(self):
        with self._condition:
            if self._closing:
                raise Error(503, "backend is closing")
            self._active += 1
            self._local.depth = getattr(self._local, "depth", 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._local.depth -= 1
                self._condition.notify_all()

    def key(self, user):
        return self.namespace + ":location:" + user_id(user)

    def channel(self, user):
        return self.namespace + ":user:" + user_id(user)

    def _routing(self, config):
        signature = (config["epoch"], tuple(sorted(config["nodes"].items())))
        with self._condition:
            if self._ring is not None and self._ring[0] == signature:
                return self._ring[1]
        ring = Ring(config["nodes"])
        with self._condition:
            self._ring = (signature, ring)
        return ring

    @contextmanager
    def _broker(self, url):
        retired = None
        with self._condition:
            if url not in self.brokers:
                if len(self.brokers) >= self.broker_capacity:
                    idle = next(
                        (key for key in self.brokers if not self._broker_users[key]),
                        None,
                    )
                    if idle is None:
                        client = None
                    else:
                        client = self._client(url)
                        retired = self.brokers.pop(idle)
                        del self._broker_users[idle]
                else:
                    client = self._client(url)
                if client is not None:
                    self.brokers[url] = client
                    self._broker_users[url] = 0
            else:
                client = self.brokers[url]
            if client is not None:
                self.brokers.move_to_end(url)
                self._broker_users[url] += 1
        # Closing a retired connection, publishing and all other network I/O run
        # outside the lifecycle/LRU lock. An active lease is never evicted.
        try:
            if retired is not None:
                with suppress(redis.RedisError, OSError):
                    retired.close()
            yield client
        finally:
            if client is not None:
                with self._condition:
                    self._broker_users[url] -= 1
                    self._condition.notify_all()

    def _publish(self, user):
        config = self.store.ring()
        if not config["nodes"]:
            return False
        ring = self._routing(config)
        url = ring.nodes[ring.owner(user)]
        try:
            with self._broker(url) as broker:
                if broker is None:
                    return False
                broker.publish(self.channel(user), dumps({"user": user}))
                return True
        except (redis.RedisError, OSError):
            return False

    def publish(self, user):
        # Hints contain no coordinates. Output always checks current SQL ACLs.
        user_id(user)
        with self._operation():
            return self._publish(user)

    def _cache_arguments(self, report):
        remaining = math.ceil(
            (report["received_at"] + self.store.ttl - self.store.clock()) * 1000
        )
        if remaining <= 0:
            return None
        return self.key(report["id"]), report["revision"], dumps(report), remaining

    def _warm(self, report):
        args = self._cache_arguments(report)
        if args is None:
            return False
        try:
            return bool(self.cache.eval(_WARM, 1, *args))
        except (redis.RedisError, OSError):
            return False  # SQL current-state recovery remains available.

    def warm(self, report):
        with self._operation():
            return self._warm(report)

    def update(self, actor, body):
        with self._operation():
            report = self.store.update(actor, body)
            self._warm(report)
            published = self._publish(actor)
            return {"revision": report["revision"], "hint_published": published}

    def _repair_cache(self, reports, deadline):
        if not reports:
            return
        try:
            cached = self.cache.mget([self.key(report["id"]) for report in reports])
        except (redis.RedisError, OSError, UnicodeError):
            # The cache is unavailable: do not spend another timeout per friend.
            return
        if not isinstance(cached, (list, tuple)):
            return
        repairs = [
            report
            for i, report in enumerate(reports)
            if i >= len(cached) or cached[i] != dumps(report)
        ]
        for offset in range(0, len(repairs), 128):
            if time.monotonic() >= deadline:
                return
            try:
                with self.cache.pipeline(transaction=False) as pipeline:
                    for report in repairs[offset : offset + 128]:
                        args = self._cache_arguments(report)
                        if args is not None:
                            pipeline.eval(_WARM, 1, *args)
                    pipeline.execute()
            except (redis.RedisError, OSError):
                return

    def snapshot(self, actor, radius=8046.72):
        try:
            valid = (
                type(radius) in (int, float)
                and math.isfinite(radius)
                and 0 < radius <= 100000
            )
        except OverflowError:
            valid = False
        if not valid:
            raise Error(400, "radius must be within (0,100000] meters")
        with self._operation():
            own, reports = self.store.reports(actor)
            if own is None:
                return []
            self._repair_cache(reports, time.monotonic() + self.repair_budget)
            # Cache calls may block while TTL, sharing, friendships or either
            # user's location changes. Re-read once after optional network work;
            # this final consistent SQL read is the response authorization point.
            own, reports = self.store.reports(actor)
            if own is None:
                return []
            result = []
            for report in reports:
                dist = meters(own, report)
                if dist <= radius + 1e-6:
                    result.append(
                        {
                            key: report[key]
                            for key in (
                                "id",
                                "latitude",
                                "longitude",
                                "timestamp",
                                "revision",
                            )
                        }
                        | {"distance_m": dist}
                    )
            return sorted(result, key=lambda row: (row["distance_m"], row["id"]))

    def close(self):
        if getattr(self._local, "depth", 0):
            raise RuntimeError("backend cannot close from its own operation")
        with self._condition:
            if self._closing:
                self._condition.wait_for(lambda: self._closed)
                return
            self._closing = True
            self._condition.wait_for(lambda: self._active == 0)
            clients = [self.cache, *self.brokers.values()]
            self.brokers.clear()
            self._broker_users.clear()
        try:
            for client in clients:
                with suppress(redis.RedisError, OSError):
                    client.close()
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()
