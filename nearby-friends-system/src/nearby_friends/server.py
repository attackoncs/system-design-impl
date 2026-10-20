import asyncio
from contextlib import suppress
import json
import logging
import time

from redis import asyncio as aioredis
from redis.exceptions import RedisError
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .core import Error, Ring, dumps


class DeliveryTimeout(Exception):
    pass


class Session:
    def __init__(self,backend,actor,ws,reconcile):
        self.backend,self.actor,self.ws,self.reconcile = backend,actor,ws,reconcile
        self.radius = 8046.72
        self.known = {}
        self.dirty = asyncio.Event()
        self.tasks = []
        self.signature = None

    async def send(self,value):
        try:
            await asyncio.wait_for(self.ws.send(dumps(value)),timeout=3)
        except asyncio.TimeoutError as exc:
            raise DeliveryTimeout() from exc

    async def watch(self,url,channels):
        while True:
            client = aioredis.Redis.from_url(url,socket_connect_timeout=0.5,socket_timeout=1)
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(*channels)
                self.dirty.set()  # Reconcile after reattachment, including missed data.
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True,timeout=0.2)
                    if message:
                        self.dirty.set()
                    await asyncio.sleep(0.01)
            except (RedisError,OSError):
                await asyncio.sleep(0.25)
            finally:
                await pubsub.aclose()
                await client.aclose()

    async def subscriptions(self):
        config = await asyncio.to_thread(self.backend.store.ring)
        friends = await asyncio.to_thread(self.backend.store.friends,self.actor)
        signature = (config["epoch"],tuple(friends))
        if signature == self.signature:
            return
        await self.stop_watchers()
        self.signature = signature
        if config["nodes"]:
            ring = Ring(config["nodes"])
            groups = {}
            for user in [self.actor,*friends]:
                node = ring.owner(user)
                groups.setdefault(ring.nodes[node],[]).append(self.backend.channel(user))
            self.tasks = [asyncio.create_task(self.watch(url,channels)) for url,channels in groups.items()]
        self.dirty.set()

    async def stop_watchers(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks = []

    async def current(self):
        return await asyncio.to_thread(self.backend.snapshot,self.actor,self.radius)

    async def deltas(self):
        current = {row["id"]:row for row in await self.current()}
        changes = []
        for identifier in sorted(self.known.keys()-current.keys()):
            changes.append({"event":"leave","id":identifier})
        for identifier,row in current.items():
            if identifier not in self.known:
                changes.append({"event":"enter","friend":row})
            elif row != self.known[identifier]:
                changes.append({"event":"update","friend":row})
        if changes:
            # Cap frames for large friendship sets while preserving ordered delivery.
            for offset in range(0,len(changes),100):
                await self.send({"type":"delta","changes":changes[offset:offset+100]})
        self.known = current

    async def dispatch(self,message):
        if not isinstance(message,dict):
            raise Error(400,"JSON object required")
        op = message.get("op")
        store = self.backend.store
        if op == "location":
            result = await asyncio.to_thread(self.backend.update,self.actor,message)
        elif op in ("friend.request","friend.accept","friend.remove"):
            result = await asyncio.to_thread(store.friendship,self.actor,message.get("target"),op)
            await asyncio.to_thread(self.backend.publish,self.actor)
        elif op in ("sharing","history.consent"):
            kind = "sharing" if op == "sharing" else "history"
            result = await asyncio.to_thread(store.setting,self.actor,kind,message.get("enabled"))
            await asyncio.to_thread(self.backend.publish,self.actor)
        elif op == "history":
            result = {"history":await asyncio.to_thread(store.read_history,self.actor,message.get("offset",0),message.get("limit",100))}
        elif op == "ring":
            result = await asyncio.to_thread(store.set_ring,self.actor,message.get("nodes"),message.get("expected_epoch"))
        elif op == "snapshot":
            radius = message.get("radius_m",self.radius)
            offset,limit = message.get("offset",0),message.get("limit",20)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
                raise Error(400,"invalid snapshot page")
            rows = await asyncio.to_thread(self.backend.snapshot,self.actor,radius)
            self.radius = radius
            result = {"friends":rows[offset:offset+limit],"total":len(rows),"offset":offset,"next_offset":offset+limit if offset+limit<len(rows) else None}
        else:
            raise Error(400,"unknown operation")
        return result

    async def run(self):
        await self.subscriptions()
        rows = await self.current()
        self.known = {row["id"]:row for row in rows}
        await self.send({"type":"snapshot","friends":rows[:20],"total":len(rows),"next_offset":20 if len(rows)>20 else None})
        last_reconcile = last_config = time.monotonic()
        try:
            while True:
                message = None
                try:
                    raw = await asyncio.wait_for(self.ws.recv(),timeout=0.1)
                    message = json.loads(raw)
                    request_id = message.get("request_id") if isinstance(message,dict) else None
                    if request_id is not None and (not isinstance(request_id,str) or len(request_id)>64):
                        raise Error(400,"invalid request_id")
                    result = await self.dispatch(message)
                    await self.send({"type":"ack","request_id":request_id,"op":message["op"],**result})
                    self.dirty.set()
                except asyncio.TimeoutError:
                    pass
                except (ValueError,TypeError,KeyError,Error) as exc:
                    await self.send({"type":"error","request_id":message.get("request_id") if isinstance(message,dict) else None,"code":exc.code if isinstance(exc,Error) else 400,"error":str(exc) if isinstance(exc,Error) else "invalid request"})
                now = time.monotonic()
                if now-last_config >= 0.5:
                    await self.subscriptions()
                    last_config = now
                if self.dirty.is_set() or now-last_reconcile >= self.reconcile:
                    self.dirty.clear()
                    await self.deltas()
                    last_reconcile = now
        finally:
            await self.stop_watchers()


async def run_server(backend,host,port,reconcile=1):
    if not 0.1 <= reconcile <= 30:
        raise ValueError("reconcile interval must be 0.1..30 seconds")
    # Suppress library access/error logs which can include application objects.
    logger = logging.getLogger("nearby.websocket")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    async def handler(ws):
        authorization = ws.request.headers.get("Authorization","")
        try:
            if not authorization.startswith("Bearer "):
                raise Error(401,"Bearer token required")
            actor = await asyncio.to_thread(backend.store.authenticate,authorization[7:])
        except Error:
            await ws.close(code=4401,reason="authentication required")
            return
        try:
            await Session(backend,actor,ws,reconcile).run()
        except (ConnectionClosed,asyncio.TimeoutError):
            pass
        except DeliveryTimeout:
            await ws.close(code=1013,reason="slow consumer")
    async def sweeper():
        while True:
            await asyncio.to_thread(backend.store.sweep)
            await asyncio.sleep(min(30,backend.store.ttl))
    cleanup = asyncio.create_task(sweeper())
    try:
        async with serve(handler,host,port,max_size=16384,max_queue=16,write_limit=32768,logger=logger):
            print(f"nearby WebSocket listening on {host}:{port}",flush=True)
            await asyncio.Future()
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
