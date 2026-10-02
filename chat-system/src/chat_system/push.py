"""Durable Redis Streams push worker with reclaim, retries, and dead letters."""
import asyncio
import json
import uuid
from dataclasses import asdict
from urllib.request import Request, urlopen


class RedisRecordingNotifier:
    def __init__(self, backend):
        self.backend = backend

    async def deliver(self, user, message, idempotency_key):
        await self.backend.redis.hsetnx(self.backend.key("push_deliveries"), idempotency_key,
                                        json.dumps({"user_id": user, "message": asdict(message)}))


class HTTPWebhookNotifier:
    """POST to a configured provider bridge; bridge must honor Idempotency-Key."""

    def __init__(self, url, bearer_token=None, timeout=5):
        if not url.startswith(("https://", "http://")) or timeout <= 0:
            raise ValueError("invalid webhook endpoint or timeout")
        self.url, self.bearer_token, self.timeout = url, bearer_token, timeout

    async def deliver(self, user, message, idempotency_key):
        body = json.dumps({"user_id": user, "message": asdict(message)}).encode()
        headers = {"Content-Type": "application/json", "Idempotency-Key": idempotency_key}
        if self.bearer_token:
            headers["Authorization"] = "Bearer " + self.bearer_token

        def post():
            request = Request(self.url, data=body, headers=headers, method="POST")
            with urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("push provider rejected delivery")

        await asyncio.to_thread(post)


class PushWorker:
    group = "offline-push"

    def __init__(self, backend, notifier=None, consumer=None, reclaim_idle_ms=15000,
                 retry_base=1.0, max_attempts=5):
        if reclaim_idle_ms < 1 or retry_base < 0 or max_attempts < 1:
            raise ValueError("invalid worker retry configuration")
        self.backend = backend
        self.notifier = notifier or RedisRecordingNotifier(backend)
        self.consumer = consumer or uuid.uuid4().hex
        self.reclaim_idle_ms, self.retry_base, self.max_attempts = reclaim_idle_ms, retry_base, max_attempts
        self.claim_cursor = "0-0"

    async def initialize(self):
        from redis.exceptions import ResponseError
        try:
            await self.backend.redis.xgroup_create(self.backend.key("push"), self.group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def _complete(self, entry_id, fields=None, error=None):
        redis = self.backend.redis
        async with redis.pipeline(transaction=True) as pipeline:
            if error:
                pipeline.xadd(self.backend.key("push_dead_letters"),
                              {**fields, "entry_id": entry_id, "error": error})
            pipeline.xack(self.backend.key("push"), self.group, entry_id)
            pipeline.hdel(self.backend.key("push_retries"), entry_id)
            await pipeline.execute()

    async def _handle(self, entry_id, fields):
        redis = self.backend.redis
        raw = await redis.hget(self.backend.key("push_retries"), entry_id)
        state = json.loads(raw) if raw else {"attempts": 0, "due": 0}
        if await self.backend.now() < state["due"]:
            return
        # Lock protects against another consumer reclaiming a slow provider invocation.
        lock = self.backend.key("push_lock:" + entry_id)
        lock_token = uuid.uuid4().hex
        if not await redis.set(lock, lock_token, nx=True, px=max(15000, self.reclaim_idle_ms * 2)):
            return
        try:
            if await self.backend.is_online(fields["user"]):
                await self._complete(entry_id)
                return
            message = await self.backend.get_message(fields["message_id"])
            identity = self.backend.prefix + fields["user"] + ":" + fields["message_id"]
            try:
                await asyncio.wait_for(self.notifier.deliver(fields["user"], message, identity), 10)
            except Exception as error:
                attempts = state["attempts"] + 1
                name = type(error).__name__
                if attempts >= self.max_attempts:
                    await self._complete(entry_id, {**fields, "attempts": str(attempts)}, name)
                else:
                    delay = min(60.0, self.retry_base * (2 ** (attempts - 1)))
                    await redis.hset(self.backend.key("push_retries"), entry_id,
                                     json.dumps({"attempts": attempts, "due": await self.backend.now() + delay,
                                                 "error": name}))
                return
            await self._complete(entry_id)
        finally:
            await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end",
                             1, lock, lock_token)

    async def run_once(self):
        await self.initialize()
        redis = self.backend.redis
        claimed = await redis.xautoclaim(self.backend.key("push"), self.group, self.consumer,
                                         min_idle_time=self.reclaim_idle_ms, start_id=self.claim_cursor, count=100)
        self.claim_cursor = claimed[0]
        entries = list(claimed[1])
        fresh = await redis.xreadgroup(self.group, self.consumer, {self.backend.key("push"): ">"}, count=100)
        for _, batch in fresh:
            entries.extend(batch)
        for entry_id, fields in entries:
            await self._handle(entry_id, fields)
        return len(entries)

    async def run(self, interval=0.2):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Broker/provider recovery is retried; pending entries remain in Redis.
                await asyncio.sleep(1)
            await asyncio.sleep(interval)
