"""Durable Redis Streams push worker with reclaim, retries, and dead
letters.
"""

import asyncio
import dataclasses
import json
import logging
import math
import urllib.request
import urllib.parse
import uuid

from chat_system.redis_scripts import (
    _PUSH_COMPLETE,
    _PUSH_LOCK,
    _PUSH_RENEW,
    _PUSH_RETRY,
)
from chat_system.concurrency import _wait_owned, run_blocking

try:
    from redis import exceptions as redis_exceptions
except ImportError:
    redis_exceptions = None  # Optional distributed extra.


class RedisRecordingNotifier:
    """Records idempotent offline delivery attempts in Redis for
    verification.
    """

    def __init__(self, backend):
        self.backend = backend

    async def deliver(self, user, message, idempotency_key):
        await self.backend.redis.hsetnx(
            self.backend.key("push_deliveries"),
            idempotency_key,
            json.dumps({"user_id": user, "message": dataclasses.asdict(message)}),
        )


class HTTPWebhookNotifier:
    """POST to a configured provider bridge; bridge must honor Idempotency-
    Key.
    """

    def __init__(self, url, bearer_token=None, timeout=5):
        endpoint = urllib.parse.urlsplit(url)
        if (
            endpoint.scheme not in ("https", "http")
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("invalid webhook endpoint or timeout")
        self.url, self.bearer_token, self.timeout = url, bearer_token, timeout

    async def deliver(self, user, message, idempotency_key):
        body = json.dumps(
            {"user_id": user, "message": dataclasses.asdict(message)}
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token

        def post():
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            request = urllib.request.Request(
                self.url, data=body, headers=headers, method="POST"
            )
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("push provider rejected delivery")

        # The provider must also honor the idempotency key. Repeated caller
        # cancellation cannot abandon an HTTP request still running in a thread.
        await run_blocking(post)


class PushWorker:
    """Consumes durable offline push events with retries and dead-letter
    handling.
    """

    group = "offline-push"

    def __init__(
        self,
        backend,
        notifier=None,
        consumer=None,
        reclaim_idle_ms=15000,
        retry_base=1.0,
        max_attempts=5,
        delivery_timeout=10.0,
    ):
        if (
            not isinstance(reclaim_idle_ms, int)
            or isinstance(reclaim_idle_ms, bool)
            or reclaim_idle_ms < 1
            or not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
            or not isinstance(retry_base, (int, float))
            or isinstance(retry_base, bool)
            or not math.isfinite(retry_base)
            or retry_base < 0
            or not isinstance(delivery_timeout, (int, float))
            or isinstance(delivery_timeout, bool)
            or not math.isfinite(delivery_timeout)
            or delivery_timeout <= 0
        ):
            raise ValueError("invalid worker retry configuration")
        self.backend = backend
        self.notifier = notifier or RedisRecordingNotifier(backend)
        self.consumer = consumer or uuid.uuid4().hex
        self.reclaim_idle_ms, self.retry_base, self.max_attempts = (
            reclaim_idle_ms,
            retry_base,
            max_attempts,
        )
        self.claim_cursor = "0-0"
        self.delivery_timeout = delivery_timeout
        self.lock_ttl_ms = max(15000, reclaim_idle_ms * 2)

    async def initialize(self):
        if redis_exceptions is None:
            raise ImportError("Install the distributed extra")

        try:
            await self.backend.redis.xgroup_create(
                self.backend.key("push"), self.group, id="0", mkstream=True
            )
        except redis_exceptions.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def _complete(self, entry_id, lock_token, fields=None, error=None):
        values = {**(fields or {}), "entry_id": entry_id, "error": error or ""}
        return await self.backend.redis.eval(
            _PUSH_COMPLETE,
            4,
            self.backend.key("push_lock:" + entry_id),
            self.backend.key("push"),
            self.backend.key("push_retries"),
            self.backend.key("push_dead_letters"),
            lock_token,
            self.group,
            entry_id,
            error or "",
            *(value for item in values.items() for value in item),
        )

    async def _renew_lock(self, lock, token):
        while True:
            await asyncio.sleep(self.lock_ttl_ms / 3000)
            renewed = await self.backend.redis.eval(
                _PUSH_RENEW,
                1,
                lock,
                token,
                self.lock_ttl_ms,
            )
            if not renewed:
                raise RuntimeError(
                    "push ownership lost; pending entry will be reclaimed"
                )

    async def _handle(self, entry_id, fields):
        redis = self.backend.redis
        # Lock protects against another consumer reclaiming a slow provider
        # invocation.
        lock = self.backend.key("push_lock:" + entry_id)
        lock_token = uuid.uuid4().hex
        if not await redis.eval(
            _PUSH_LOCK,
            2,
            self.backend.key("push"),
            lock,
            self.group,
            entry_id,
            lock_token,
            self.lock_ttl_ms,
        ):
            return
        renewal = asyncio.create_task(self._renew_lock(lock, lock_token))
        delivery = asyncio.create_task(
            self._deliver_locked(entry_id, fields, lock, lock_token)
        )
        try:
            done, _ = await asyncio.wait(
                (renewal, delivery), return_when=asyncio.FIRST_COMPLETED
            )
            if renewal in done:
                await renewal
            await delivery
        finally:
            await _wait_owned(
                asyncio.create_task(
                    self._release_lock(lock, lock_token, delivery, renewal)
                )
            )

    async def _release_lock(self, lock, lock_token, delivery, renewal):
        delivery.cancel()
        # Keep renewing until cancellation-sensitive providers have drained
        # their in-flight I/O. A stale lock can never acknowledge a new owner.
        await asyncio.gather(delivery, return_exceptions=True)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        await self.backend.redis.eval(
            "if redis.call('GET',KEYS[1])==ARGV[1] then return"
            " redis.call('DEL',KEYS[1]) end",
            1,
            lock,
            lock_token,
        )

    async def _deliver_locked(self, entry_id, fields, lock, lock_token):
        redis = self.backend.redis
        # State must be read after acquiring the lock: an earlier consumer
        # may have committed another failed attempt before releasing it.
        raw = await redis.hget(self.backend.key("push_retries"), entry_id)
        state = json.loads(raw) if raw else {"attempts": 0, "due": 0}
        if await self.backend.now() < state["due"]:
            return
        if await self.backend.is_online(fields["user"]):
            await self._complete(entry_id, lock_token)
            return
        message = await self.backend.get_message(fields["message_id"])
        identity = self.backend.prefix + fields["user"] + ":" + fields["message_id"]
        try:
            await asyncio.wait_for(
                self.notifier.deliver(fields["user"], message, identity),
                self.delivery_timeout,
            )
        # Isolate this operation; record failures before continuing.
        except Exception as error:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception("Failure isolated in %s", "_handle")
            attempts = state["attempts"] + 1
            name = type(error).__name__
            if attempts >= self.max_attempts:
                await self._complete(
                    entry_id, lock_token, {**fields, "attempts": str(attempts)}, name
                )
            else:
                delay = min(60.0, self.retry_base * (2 ** min(attempts - 1, 20)))
                await redis.eval(
                    _PUSH_RETRY,
                    3,
                    lock,
                    self.backend.key("push"),
                    self.backend.key("push_retries"),
                    lock_token,
                    self.group,
                    entry_id,
                    json.dumps(
                        {
                            "attempts": attempts,
                            "due": await self.backend.now() + delay,
                            "error": name,
                        }
                    ),
                )
            return
        await self._complete(entry_id, lock_token)

    async def run_once(self):
        await self.initialize()
        redis = self.backend.redis
        claimed = await redis.xautoclaim(
            self.backend.key("push"),
            self.group,
            self.consumer,
            min_idle_time=self.reclaim_idle_ms,
            start_id=self.claim_cursor,
            count=100,
        )
        self.claim_cursor = claimed[0]
        entries = list(claimed[1])
        fresh = await redis.xreadgroup(
            self.group,
            self.consumer,
            {self.backend.key("push"): ">"},
            count=100,
        )
        for _, batch in fresh:
            entries.extend(batch)
        for entry_id, fields in entries:
            await self._handle(entry_id, fields)
        return len(entries)

    async def run(self, interval=0.2):
        if (
            not isinstance(interval, (int, float))
            or isinstance(interval, bool)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("interval must be finite and positive")
        while True:
            try:
                await self.run_once()
            # Isolate this operation; record failures before continuing.
            except Exception:  # pylint: disable=broad-exception-caught
                # Broker/provider recovery is retried; pending entries remain in
                # Redis.
                logging.getLogger(__name__).exception("Failure isolated in %s", "run")
                await asyncio.sleep(1)
            await asyncio.sleep(interval)
