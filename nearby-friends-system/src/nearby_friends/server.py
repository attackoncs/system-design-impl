import asyncio
from contextlib import suppress
from http import HTTPStatus
import math
import logging
import time

from redis import asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .core import Error, Ring, dumps
from .protocol import MAX_MESSAGE, MAX_OUTPUT, parse, validate
from .work import WorkPool, finish


class DeliveryTimeout(Exception):
    pass


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class Session:
    def __init__(self, backend, actor, ws, reconcile, *, work=None, send_timeout=3):
        if not _finite(reconcile) or not 0.1 <= reconcile <= 30:
            raise ValueError("reconcile interval must be 0.1..30 seconds")
        if not _finite(send_timeout) or not 0 < send_timeout <= 30:
            raise ValueError("send timeout must be finite and in (0,30] seconds")
        self.backend, self.actor, self.ws, self.reconcile = (
            backend,
            actor,
            ws,
            reconcile,
        )
        self.radius = 8046.72
        self.known = {}
        self.dirty = asyncio.Event()
        self.tasks = []
        self.signature = None
        self.work = work if work is not None else WorkPool(1)
        self._owns_work = work is None
        self.send_timeout = send_timeout
        self._watchers = {}

    async def send(self, value, *, deadline=None):
        payload = dumps(value)
        if len(payload.encode("utf-8")) > MAX_OUTPUT:
            raise Error(413, "response too large; request a smaller page")
        timeout = self.send_timeout
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise DeliveryTimeout()
        try:
            await asyncio.wait_for(self.ws.send(payload), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DeliveryTimeout() from exc

    async def watch(self, url, channels):
        while True:
            client = aioredis.Redis.from_url(
                url,
                socket_connect_timeout=0.5,
                socket_timeout=1,
                max_connections=1,
                retry=Retry(NoBackoff(), 0),
                retry_on_timeout=False,
            )
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(*channels)
                self.dirty.set()  # Reconcile after reattachment, including missed data.
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.2
                    )
                    if message:
                        self.dirty.set()
                    await asyncio.sleep(0.01)
            except (RedisError, OSError):
                await asyncio.sleep(0.25)
            finally:
                await finish(
                    asyncio.gather(
                        pubsub.aclose(), client.aclose(), return_exceptions=True
                    )
                )

    async def subscriptions(self):
        config = await self.work.call(self.backend.store.ring)
        friends = await self.work.call(self.backend.store.friends, self.actor)
        signature = (config["epoch"], tuple(friends))
        if signature == self.signature and all(not task.done() for task in self.tasks):
            return
        groups = {}
        if config["nodes"]:
            ring = Ring(config["nodes"])
            for user in [self.actor, *friends]:
                node = ring.owner(user)
                groups.setdefault(ring.nodes[node], []).append(
                    self.backend.channel(user)
                )
        desired = {(url, tuple(channels)) for url, channels in groups.items()}
        retained = {
            key: task
            for key, task in self._watchers.items()
            if key in desired and not task.done()
        }
        for key in desired - retained.keys():
            retained[key] = asyncio.create_task(self.watch(*key))
        retired = [
            task
            for key, task in self._watchers.items()
            if retained.get(key) is not task
        ]
        self._watchers = retained
        self.tasks = list(retained.values())
        self.signature = signature
        for task in retired:
            task.cancel()
        await finish(asyncio.gather(*retired, return_exceptions=True))
        self.dirty.set()

    async def stop_watchers(self):
        for task in self.tasks:
            task.cancel()
        await finish(asyncio.gather(*self.tasks, return_exceptions=True))
        self.tasks = []
        self._watchers = {}

    async def current(self):
        return await self.work.call(self.backend.snapshot, self.actor, self.radius)

    async def deltas(self):
        current = {row["id"]: row for row in await self.current()}
        changes = []
        for identifier in sorted(self.known.keys() - current.keys()):
            changes.append({"event": "leave", "id": identifier})
        for identifier, row in current.items():
            if identifier not in self.known:
                changes.append({"event": "enter", "friend": row})
            elif row != self.known[identifier]:
                changes.append({"event": "update", "friend": row})
        if changes:
            # A large friendship set uses bounded frames and one total sending
            # budget, rather than multiplying slow-consumer latency by pages.
            deadline = time.monotonic() + self.send_timeout
            for offset in range(0, len(changes), 100):
                await self.send(
                    {"type": "delta", "changes": changes[offset : offset + 100]},
                    deadline=deadline,
                )
        self.known = current

    async def dispatch(self, message):
        validate(message)
        op = message["op"]
        store = self.backend.store
        if op == "location":
            result = await self.work.call(
                self.backend.update,
                self.actor,
                {
                    key: value
                    for key, value in message.items()
                    if key not in ("op", "request_id")
                },
            )
        elif op in ("friend.request", "friend.accept", "friend.remove"):
            result = await self.work.call(
                store.friendship, self.actor, message.get("target"), op
            )
            await self.work.call(self.backend.publish, self.actor)
        elif op in ("sharing", "history.consent"):
            kind = "sharing" if op == "sharing" else "history"
            result = await self.work.call(
                store.setting, self.actor, kind, message.get("enabled")
            )
            await self.work.call(self.backend.publish, self.actor)
        elif op == "history":
            result = {
                "history": await self.work.call(
                    store.read_history,
                    self.actor,
                    message.get("offset", 0),
                    message.get("limit", 100),
                )
            }
        elif op == "ring":
            result = await self.work.call(
                store.set_ring,
                self.actor,
                message.get("nodes"),
                message.get("expected_epoch"),
            )
        elif op == "snapshot":
            radius = message.get("radius_m", self.radius)
            offset, limit = message.get("offset", 0), message.get("limit", 20)
            if (
                type(offset) is not int
                or offset < 0
                or type(limit) is not int
                or not 1 <= limit <= 100
            ):
                raise Error(400, "invalid snapshot page")
            rows = await self.work.call(self.backend.snapshot, self.actor, radius)
            self.radius = radius
            # This acknowledgement contains only one page. Preserve the full
            # delta baseline so changed radius/pages cannot swallow events for
            # friends the client already loaded from another page.
            result = {
                "friends": rows[offset : offset + limit],
                "total": len(rows),
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(rows) else None,
            }
        else:
            raise Error(400, "unknown operation")
        return result

    async def run(self):
        try:
            await self.subscriptions()
            rows = await self.current()
            self.known = {row["id"]: row for row in rows}
            await self.send(
                {
                    "type": "snapshot",
                    "friends": rows[:20],
                    "total": len(rows),
                    "next_offset": 20 if len(rows) > 20 else None,
                }
            )
            last_reconcile = last_config = time.monotonic()
            while True:
                message = None
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=0.1)
                    message = parse(raw)
                    result = await self.dispatch(message)
                    await self.send(
                        {
                            "type": "ack",
                            "request_id": message.get("request_id"),
                            "op": message["op"],
                            **result,
                        }
                    )
                    self.dirty.set()
                except asyncio.TimeoutError:
                    pass
                except (
                    ValueError,
                    TypeError,
                    KeyError,
                    OverflowError,
                    RecursionError,
                    Error,
                ) as exc:
                    await self.send(
                        {
                            "type": "error",
                            "request_id": message.get("request_id")
                            if isinstance(message, dict)
                            and isinstance(message.get("request_id"), str)
                            and 1 <= len(message["request_id"]) <= 64
                            else None,
                            "code": exc.code if isinstance(exc, Error) else 400,
                            "error": str(exc)
                            if isinstance(exc, Error)
                            else "invalid request",
                        }
                    )
                now = time.monotonic()
                if now - last_config >= 0.5:
                    await self.subscriptions()
                    last_config = now
                if self.dirty.is_set() or now - last_reconcile >= self.reconcile:
                    self.dirty.clear()
                    await self.deltas()
                    last_reconcile = now
        finally:
            await self.close()

    async def close(self):
        try:
            await self.stop_watchers()
        finally:
            if self._owns_work:
                await self.work.close()


async def run_server(
    backend,
    host,
    port,
    reconcile=1,
    *,
    stop=None,
    max_connections=256,
    workers=16,
    send_timeout=3,
):
    """Serve admitted sessions, then close sockets and drain accepted store I/O.

    stop is an optional asyncio.Event for controlled shutdown. The connection
    limit covers upgraded application sessions; the transport additionally has
    a finite opening timeout/backlog. Neither limit is an OS socket quota.
    """
    if not _finite(reconcile) or not 0.1 <= reconcile <= 30:
        raise ValueError("reconcile interval must be 0.1..30 seconds")
    if type(max_connections) is not int or not 1 <= max_connections <= 10000:
        raise ValueError("max_connections must be an integer in 1..10000")
    if not _finite(send_timeout) or not 0 < send_timeout <= 30:
        raise ValueError("send timeout must be finite and in (0,30] seconds")
    work = WorkPool(workers)
    logger = logging.getLogger("nearby.websocket")
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    active = 0
    draining = False

    def process_request(connection, request):
        # Avoid upgrading when overload is already visible. Simultaneous
        # handshakes still recheck the admission count in handler below.
        if draining or active >= max_connections:
            return connection.respond(
                HTTPStatus.SERVICE_UNAVAILABLE, "service capacity reached\n"
            )
        return None

    async def handler(ws):
        nonlocal active
        if draining or active >= max_connections:
            await ws.close(code=1013, reason="service capacity reached")
            return
        active += 1
        try:
            try:
                values = ws.request.headers.get_all("Authorization")
                if len(values) != 1 or not values[0].startswith("Bearer "):
                    raise Error(401, "Bearer token required")
                actor = await work.call(backend.store.authenticate, values[0][7:])
            except Error:
                await ws.close(code=4401, reason="authentication required")
                return
            await Session(
                backend, actor, ws, reconcile, work=work, send_timeout=send_timeout
            ).run()
        except (ConnectionClosed, asyncio.TimeoutError):
            pass
        except DeliveryTimeout:
            await ws.close(code=1013, reason="slow consumer")
        except Exception:
            # Never place message bodies, coordinates or credentials in logs.
            await ws.close(code=1011, reason="service temporarily unavailable")
        finally:
            active -= 1

    async def sweeper():
        while True:
            try:
                await work.call(backend.store.sweep)
            except Exception:
                logger.warning("nearby cleanup failed; will retry")
            await asyncio.sleep(min(30, backend.store.ttl))

    cleanup = None
    try:
        async with serve(
            handler,
            host,
            port,
            max_size=MAX_MESSAGE,
            max_queue=16,
            write_limit=32768,
            logger=logger,
            open_timeout=5,
            close_timeout=3,
            backlog=128,
            process_request=process_request,
        ):
            # Bind first: a port failure must not leave maintenance work running.
            cleanup = asyncio.create_task(sweeper())
            print(f"nearby WebSocket listening on {host}:{port}", flush=True)
            try:
                if stop is None:
                    await asyncio.Future()
                else:
                    await stop.wait()
            finally:
                draining = True
    finally:
        try:
            if cleanup is not None:
                cleanup.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup
        finally:
            await work.close()
