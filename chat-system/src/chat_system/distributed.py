"""Independent chat node using shared Redis persistence and stream fanout."""

import asyncio
import contextlib
import dataclasses
import json
import logging
import uuid

import chat_system.models
import chat_system.redis_backend
import chat_system.service


class DistributedChatService:
    """A leased chat node routing durable Redis events to local devices."""

    def __init__(
        self,
        backend,
        node_id,
        url,
        region="local",
        capacity=1000,
        node_lease=10.0,
        poll_interval=0.2,
        config=None,
    ):
        if capacity < 1 or node_lease <= 0 or not 0 < poll_interval < node_lease / 2:
            raise ValueError("invalid capacity or lease intervals")
        self.backend = backend
        self.store = backend
        self.auth = backend
        self.node_id = chat_system.redis_backend.identifier(node_id)
        self.instance = uuid.uuid4().hex
        self.url, self.region, self.capacity = url, region, capacity
        self.node_lease, self.poll_interval = node_lease, poll_interval
        self.config = config or chat_system.models.ChatConfig()
        self._sessions = {}
        self._user_sessions = {}
        self._subscriptions = {}
        self._tasks = []
        self.background_error = None
        self.cursor = "0-0"
        self.started = False

    async def start(self):
        """Acquire the node lease, start background tasks, and return this
        service.
        """
        await self.backend.redis.ping()
        saved = await self.backend.redis.hget(
            self.backend.key("node_cursors"), self.node_id
        )
        if saved:
            self.cursor = saved
        else:
            last = await self.backend.redis.xrevrange(
                self.backend.key("events"), count=1
            )
            self.cursor = last[0][0] if last else "0-0"
        if not await self._renew():
            raise chat_system.models.ChatError(
                "node ID already leased by another process"
            )
        self.started = True
        self._tasks = [
            asyncio.create_task(self._maintenance()),
            asyncio.create_task(self._consume()),
        ]
        return self

    async def _renew(self):
        return await self.backend.renew_node(
            self.node_id,
            self.instance,
            self.url,
            self.region,
            self.capacity,
            self.node_lease,
        )

    async def close(self):
        """Stop background tasks, disconnect devices, and release the node
        lease.
        """
        self.started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
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
        session.queue.put_nowait(
            {"type": "closed", "reason": "rediscover, reconnect and sync"}
        )

    async def _enqueue(self, session, event):
        if session.closed:
            return
        try:
            session.queue.put_nowait(event)
        except asyncio.QueueFull:
            await self.disconnect(session)

    async def connect(self, token, device_id):
        """Authenticate and attach a device, replacing its previous session.

        Args:
            token: Previously issued bearer token.
            device_id: Nonempty identifier unique to this user's device.

        Returns:
            The active Session used by subsequent operations.

        Raises:
            AuthenticationError: The token is invalid.
            ChatError: The device is invalid or admission fails.
        """
        if not self.started:
            raise chat_system.models.ChatError("node not ready")
        user = await self.auth.authenticate(token)
        chat_system.redis_backend.identifier(device_id)
        session = chat_system.service.Session(
            user,
            device_id,
            token,
            asyncio.Queue(self.config.session_queue_size),
        )
        session.session_id = uuid.uuid4().hex
        admitted = await self.backend.admit(
            user,
            device_id,
            session.session_id,
            self.node_id,
            self.instance,
            self.config.heartbeat_timeout,
            self.capacity,
        )
        if not admitted:
            raise chat_system.models.ChatError("node full or lease expired; rediscover")
        previous = self._sessions.get((user, device_id))
        if previous:
            self._local_close(previous)
        self._sessions[(user, device_id)] = session
        self._user_sessions.setdefault(user, set()).add(session)
        return session

    async def validate_session(self, session):
        if (
            session.closed
            or self._sessions.get((session.user_id, session.device_id)) is not session
        ):
            raise chat_system.models.AuthenticationError("session closed")
        valid = await self.backend.session_operation(
            session.user_id,
            session.device_id,
            session.session_id,
            self.node_id,
            instance=self.instance,
            token=session.token,
        )
        if not valid:
            self._local_close(session)
            raise chat_system.models.AuthenticationError("expired or replaced session")

    async def heartbeat(self, session):
        """Refresh the lease of an authenticated active session.

        Args:
            session: Session returned by connect.

        Raises:
            AuthenticationError: The session has expired or been replaced.
        """
        if (
            session.closed
            or self._sessions.get((session.user_id, session.device_id)) is not session
        ):
            raise chat_system.models.AuthenticationError("session closed")
        valid = await self.backend.session_operation(
            session.user_id,
            session.device_id,
            session.session_id,
            self.node_id,
            "heartbeat",
            self.config.heartbeat_timeout,
            instance=self.instance,
            token=session.token,
        )
        if not valid:
            self._local_close(session)
            raise chat_system.models.AuthenticationError("expired or replaced session")

    async def disconnect(self, session):
        """Close a session and publish offline presence when appropriate.

        Args:
            session: Session to close; repeated disconnects are harmless.
        """
        if not session.closed:
            try:
                await self.backend.session_operation(
                    session.user_id,
                    session.device_id,
                    session.session_id,
                    self.node_id,
                    "close",
                    instance=self.instance,
                )
            finally:
                self._local_close(session)

    async def expire_sessions(self):
        """Disconnect expired or revoked sessions and update presence."""
        sessions = list(self._sessions.values())
        valid = await self.backend.check_sessions(sessions, self.node_id, self.instance)
        for session, alive in zip(sessions, valid):
            if not alive:
                await self.disconnect(session)

    async def is_online(self, user):
        """Return whether user_id currently has a live device session."""
        return await self.backend.is_online(user)

    async def subscribe_presence(self, session, user_ids):
        """Replace a session's subscriptions and return current online states.

        Args:
            session: Authenticated session receiving presence events.
            user_ids: At most 100 nonempty user identifiers.

        Returns:
            A mapping from subscribed user IDs to online booleans.

        Raises:
            ChatError: The subscription or session is invalid.
        """
        await self.validate_session(session)
        users = {chat_system.redis_backend.identifier(user) for user in user_ids}
        if len(users) > 100:
            raise chat_system.models.ChatError("too many subscriptions")
        snapshot = {user: await self.is_online(user) for user in users}
        self._subscriptions[session] = snapshot
        return dict(snapshot)

    async def create_direct(self, session, recipient):
        """Return the direct channel between the session user and recipient.

        Args:
            session: Authenticated caller session.
            recipient: The other user's identifier.

        Returns:
            An existing or newly created two-member Channel.
        """
        await self.validate_session(session)
        return await self.store.create_channel(
            session.user_id, [recipient], "direct", 2
        )

    async def create_group(self, session, members):
        """Create a group owned by the authenticated session user.

        Args:
            session: Authenticated owner session.
            members: Initial member IDs; the owner is included automatically.

        Returns:
            The new Channel.

        Raises:
            ChatError: The member list violates the configured group limit.
        """
        await self.validate_session(session)
        return await self.store.create_channel(
            session.user_id, members, "group", self.config.max_group_members
        )

    async def add_member(self, session, channel_id, user_id):
        """Add a user to a group on behalf of its owner.

        Args:
            session: Authenticated group owner session.
            channel_id: Existing group identifier.
            user_id: Identifier of the user to add.

        Returns:
            The updated Channel.

        Raises:
            ChatError: The caller lacks ownership or the group is full.
        """
        await self.validate_session(session)
        return await self.store.add_member(
            channel_id, session.user_id, user_id, self.config.max_group_members
        )

    async def send(self, session, channel_id, content, client_message_id):
        """Persist a message idempotently and arrange recipient delivery.

        Args:
            session: Authenticated sender session.
            channel_id: Channel to which the sender belongs.
            content: Nonempty message text within the configured length limit.
            client_message_id: Sender-scoped retry key, at most 200 characters.

        Returns:
            The persisted Message, including the original ID on an identical
            retry.

        Raises:
            ChatError: Input, channel access, or an idempotency key is invalid.
        """
        await self.validate_session(session)
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self.config.max_message_length
        ):
            raise chat_system.models.ChatError("invalid content")
        chat_system.redis_backend.identifier(client_message_id)
        message, _, _ = await self.store.append(
            channel_id,
            session.user_id,
            content,
            client_message_id,
            session=session,
            node=self.node_id,
            instance=self.instance,
        )
        # Broker publication is committed. Every node, including this one,
        # consumes in log order.
        return message

    async def sync(self, session, after=0, limit=100):
        """Return an ascending page of the session user's durable inbox.

        Args:
            session: Authenticated device session.
            after: Exclusive message ID cursor; zero starts from the beginning.
            limit: Maximum number of messages to return.

        Returns:
            Messages after the cursor, ordered by message ID.
        """
        await self.validate_session(session)
        return await self.store.sync(session.user_id, after, limit)

    async def history(self, session, channel_id, after=0, limit=100):
        """Return an ascending page of an accessible channel's messages.

        Args:
            session: Authenticated channel member session.
            channel_id: Channel whose history should be read.
            after: Exclusive message ID cursor.
            limit: Maximum number of messages to return.

        Returns:
            Messages visible to the caller after the cursor.
        """
        await self.validate_session(session)
        return await self.store.history(channel_id, session.user_id, after, limit)

    async def _consume(self):
        while True:
            try:
                batches = await self.backend.redis.xread(
                    {self.backend.key("events"): self.cursor},
                    count=100,
                    block=1000,
                )
                for _, entries in batches:
                    deliveries = []
                    recipients = set()
                    for event_id, fields in entries:
                        message = chat_system.redis_backend.message_from_json(
                            fields["data"]
                        )
                        members = set(json.loads(fields["members"]))
                        sessions = []
                        for member in members:
                            sessions.extend(self._user_sessions.get(member, ()))
                        recipients.update(sessions)
                        deliveries.append((message, sessions))
                    recipients = list(recipients)
                    checks = await self.backend.check_sessions(
                        recipients, self.node_id, self.instance
                    )
                    valid = {
                        session for session, alive in zip(recipients, checks) if alive
                    }
                    for session, alive in zip(recipients, checks):
                        if not alive:
                            await self.disconnect(session)
                    for message, sessions in deliveries:
                        for session in sessions:
                            if session in valid:
                                await self._enqueue(
                                    session,
                                    {
                                        "type": "message",
                                        "message": dataclasses.asdict(message),
                                    },
                                )
                    # Checkpoint after the entire bounded batch. A crash may
                    # replay
                    # up to 100 hints; the durable inbox remains authoritative.
                    event_id = entries[-1][0]
                    if not await self.backend.checkpoint(
                        self.node_id, self.instance, event_id
                    ):
                        raise chat_system.models.ChatError(
                            "node lease unavailable; retry after renewal"
                        )
                    self.cursor = event_id
                self.background_error = None
            # Isolate this operation; record failures before continuing.
            except Exception as error:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_consume"
                )
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
                            await self._enqueue(
                                session,
                                {
                                    "type": "presence",
                                    "user_id": user,
                                    "online": online,
                                },
                            )
                self.background_error = None
            # Isolate this operation; record failures before continuing.
            except Exception as error:  # pylint: disable=broad-exception-caught
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_maintenance"
                )
                self.background_error = type(error).__name__
            await asyncio.sleep(self.poll_interval)
