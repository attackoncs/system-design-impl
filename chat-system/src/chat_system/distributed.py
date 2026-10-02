"""Independent chat node using shared Redis persistence and stream fanout."""
import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import asdict

from .models import AuthenticationError, ChatConfig, ChatError
from .redis_backend import identifier, message_from_json
from .service import Session


class DistributedChatService:
    def __init__(self, backend, node_id, url, region="local", capacity=1000,
                 node_lease=10.0, poll_interval=0.2, config=None):
        if capacity < 1 or node_lease <= 0 or not 0 < poll_interval < node_lease / 2:
            raise ValueError("invalid capacity or lease intervals")
        self.backend = backend
        self.store = backend
        self.auth = backend
        self.node_id = identifier(node_id)
        self.instance = uuid.uuid4().hex
        self.url, self.region, self.capacity = url, region, capacity
        self.node_lease, self.poll_interval = node_lease, poll_interval
        self.config = config or ChatConfig()
        self._sessions = {}
        self._user_sessions = {}
        self._subscriptions = {}
        self._tasks = []
        self.background_error = None
        self.cursor = "0-0"
        self.started = False

    async def start(self):
        await self.backend.redis.ping()
        saved = await self.backend.redis.hget(self.backend.key("node_cursors"), self.node_id)
        if saved:
            self.cursor = saved
        else:
            last = await self.backend.redis.xrevrange(self.backend.key("events"), count=1)
            self.cursor = last[0][0] if last else "0-0"
        if not await self._renew():
            raise ChatError("node ID already leased by another process")
        self.started = True
        self._tasks = [asyncio.create_task(self._maintenance()), asyncio.create_task(self._consume())]
        return self

    async def _renew(self):
        return await self.backend.renew_node(self.node_id, self.instance, self.url, self.region,
                                             self.capacity, self.node_lease)

    async def close(self):
        self.started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for session in list(self._sessions.values()):
            await self.disconnect(session)
        await self.backend.remove_node(self.node_id, self.instance)

    def _local_close(self, session):
        session.closed = True
        key = (session.user_id, session.device_id)
        if self._sessions.get(key) is session:
            self._sessions.pop(key, None)
        devices = self._user_sessions.get(session.user_id)
        if devices is not None:
            devices.discard(session)
            if not devices:
                self._user_sessions.pop(session.user_id, None)
        self._subscriptions.pop(session, None)
        while not session.queue.empty():
            session.queue.get_nowait()
        session.queue.put_nowait({"type": "closed", "reason": "rediscover, reconnect and sync"})

    async def _enqueue(self, session, event):
        if session.closed:
            return
        try:
            session.queue.put_nowait(event)
        except asyncio.QueueFull:
            await self.disconnect(session)

    async def connect(self, token, device_id):
        if not self.started:
            raise ChatError("node not ready")
        user = await self.auth.authenticate(token)
        identifier(device_id)
        session = Session(user, device_id, token, asyncio.Queue(self.config.session_queue_size))
        session.session_id = uuid.uuid4().hex
        admitted = await self.backend.admit(user, device_id, session.session_id, self.node_id,
                                            self.instance, self.config.heartbeat_timeout, self.capacity)
        if not admitted:
            raise ChatError("node full or lease expired; rediscover")
        previous = self._sessions.get((user, device_id))
        if previous:
            self._local_close(previous)
        self._sessions[(user, device_id)] = session
        self._user_sessions.setdefault(user, set()).add(session)
        return session

    async def _require(self, session):
        if session.closed or self._sessions.get((session.user_id, session.device_id)) is not session:
            raise AuthenticationError("session closed")
        valid = await self.backend.session_operation(session.user_id, session.device_id,
                    session.session_id, self.node_id, instance=self.instance, token=session.token)
        if not valid:
            self._local_close(session)
            raise AuthenticationError("expired or replaced session")

    async def heartbeat(self, session):
        if session.closed or self._sessions.get((session.user_id, session.device_id)) is not session:
            raise AuthenticationError("session closed")
        valid = await self.backend.session_operation(session.user_id, session.device_id,
                    session.session_id, self.node_id, "heartbeat", self.config.heartbeat_timeout,
                    instance=self.instance, token=session.token)
        if not valid:
            self._local_close(session)
            raise AuthenticationError("expired or replaced session")

    async def disconnect(self, session):
        if not session.closed:
            try:
                await self.backend.session_operation(session.user_id, session.device_id, session.session_id,
                                                     self.node_id, "close", instance=self.instance)
            finally:
                self._local_close(session)

    async def expire_sessions(self):
        sessions = list(self._sessions.values())
        valid = await self.backend.check_sessions(sessions, self.node_id, self.instance)
        for session, alive in zip(sessions, valid):
            if not alive:
                await self.disconnect(session)

    async def is_online(self, user):
        return await self.backend.is_online(user)

    async def subscribe_presence(self, session, user_ids):
        await self._require(session)
        users = {identifier(user) for user in user_ids}
        if len(users) > 100:
            raise ChatError("too many subscriptions")
        snapshot = {user: await self.is_online(user) for user in users}
        self._subscriptions[session] = snapshot
        return dict(snapshot)

    async def create_direct(self, session, recipient):
        await self._require(session)
        return await self.store.create_channel(session.user_id, [recipient], "direct", 2)

    async def create_group(self, session, members):
        await self._require(session)
        return await self.store.create_channel(session.user_id, members, "group", self.config.max_group_members)

    async def add_member(self, session, channel_id, user_id):
        await self._require(session)
        return await self.store.add_member(channel_id, session.user_id, user_id, self.config.max_group_members)

    async def send(self, session, channel_id, content, client_message_id):
        await self._require(session)
        if not isinstance(content, str) or not content.strip() or len(content) > self.config.max_message_length:
            raise ChatError("invalid content")
        identifier(client_message_id)
        message, _, _ = await self.store.append(channel_id, session.user_id, content, client_message_id,
                                               session=session, node=self.node_id, instance=self.instance)
        # Broker publication is committed. Every node, including this one, consumes in log order.
        return message

    async def sync(self, session, after=0, limit=100):
        await self._require(session)
        return await self.store.sync(session.user_id, after, limit)

    async def history(self, session, channel_id, after=0, limit=100):
        await self._require(session)
        return await self.store.history(channel_id, session.user_id, after, limit)

    async def _consume(self):
        while True:
            try:
                batches = await self.backend.redis.xread({self.backend.key("events"): self.cursor},
                                                         count=100, block=1000)
                for _, entries in batches:
                    deliveries = []
                    recipients = set()
                    for event_id, fields in entries:
                        message = message_from_json(fields["data"])
                        members = set(json.loads(fields["members"]))
                        sessions = []
                        for member in members:
                            sessions.extend(self._user_sessions.get(member, ()))
                        recipients.update(sessions)
                        deliveries.append((message, sessions))
                    recipients = list(recipients)
                    checks = await self.backend.check_sessions(recipients, self.node_id, self.instance)
                    valid = {session for session, alive in zip(recipients, checks) if alive}
                    for session, alive in zip(recipients, checks):
                        if not alive:
                            await self.disconnect(session)
                    for message, sessions in deliveries:
                        for session in sessions:
                            if session in valid:
                                await self._enqueue(session, {"type": "message", "message": asdict(message)})
                    # Checkpoint after the entire bounded batch. A crash may replay
                    # up to 100 hints; the durable inbox remains authoritative.
                    event_id = entries[-1][0]
                    if not await self.backend.checkpoint(self.node_id, self.instance, event_id):
                        raise ChatError("node lease unavailable; retry after renewal")
                    self.cursor = event_id
                self.background_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.background_error = type(error).__name__
                await asyncio.sleep(self.poll_interval)

    async def _maintenance(self):
        while True:
            try:
                if not await self._renew():
                    for session in list(self._sessions.values()):
                        self._local_close(session)
                    return
                await self.expire_sessions()
                await self.backend.prune_sessions()
                for session, snapshot in list(self._subscriptions.items()):
                    for user, old in list(snapshot.items()):
                        online = await self.is_online(user)
                        if online != old:
                            snapshot[user] = online
                            await self._enqueue(session, {"type": "presence", "user_id": user, "online": online})
                self.background_error = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.background_error = type(error).__name__
            await asyncio.sleep(self.poll_interval)
