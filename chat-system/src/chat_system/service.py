"""Local chat orchestration with indexed sessions and nonblocking persistence.

Session maps and live queues belong to one event loop. Store methods may run in
worker threads; synchronous convenience methods remain available to library users.
"""

from __future__ import annotations

import asyncio
import collections
import copy
import dataclasses
import hashlib
import logging
import secrets
import threading
import time
from typing import Callable, Optional, Protocol

from chat_system.concurrency import gather_owned, run_blocking
from chat_system.models import (
    AuthenticationError,
    ChatConfig,
    ChatError,
    Message,
    validate_user_id,
)
from chat_system.store import MessageStore, SQLiteMessageStore

logger = logging.getLogger(__name__)


class TokenAuthenticator:
    """Provision random bearer tokens; only their digests are retained locally."""

    def __init__(self):
        self._tokens: dict[bytes, str] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _digest(token):
        if (
            not isinstance(token, str)
            or not token.isascii()
            or not 1 <= len(token) <= 256
        ):
            raise AuthenticationError("invalid token")
        return hashlib.sha256(token.encode("ascii")).digest()

    def issue(self, user_id: str) -> str:
        validate_user_id(user_id)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[self._digest(token)] = user_id
        return token

    def authenticate(self, token) -> str:
        digest = self._digest(token)
        with self._lock:
            user = self._tokens.get(digest)
        if user is None:
            raise AuthenticationError("invalid token")
        return user

    def revoke(self, token) -> None:
        try:
            digest = self._digest(token)
        except AuthenticationError:
            return
        with self._lock:
            self._tokens.pop(digest, None)


@dataclasses.dataclass(eq=False)
class Session:
    """A device connection with a bounded live-event queue."""

    user_id: str
    device_id: str
    token: str = dataclasses.field(repr=False)
    queue: asyncio.Queue = dataclasses.field(repr=False)
    last_heartbeat: float = 0.0
    closed: bool = False


class Notifier(Protocol):
    async def notify(self, user_id: str, message: Message) -> None: ...


class RecordingNotifier:
    """Bounded local diagnostic adapter; replace with an asynchronous provider."""

    def __init__(self, capacity: int = 1000):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = capacity
        self.notifications = []

    async def notify(self, user_id: str, message: Message) -> None:
        self.notifications.append((user_id, message))
        if len(self.notifications) > self.capacity:
            del self.notifications[0]


class ChatService:
    """One-loop session/presence owner with durable inboxes as source of truth."""

    def __init__(
        self,
        store: Optional[MessageStore] = None,
        authenticator=None,
        notifier: Optional[Notifier] = None,
        config: Optional[ChatConfig] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = store if store is not None else SQLiteMessageStore()
        self.auth = authenticator if authenticator is not None else TokenAuthenticator()
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.config = config or ChatConfig()
        self.clock = clock
        self._sessions: dict[tuple[str, str], Session] = {}
        self._user_sessions: dict[str, set[Session]] = {}
        self._subscriptions: dict[Session, set[str]] = {}
        self._watchers: dict[str, set[Session]] = {}
        self._presence_events = collections.deque()
        self._publishing_presence = False
        self._send_lock = asyncio.Lock()
        self._push_slots = asyncio.Semaphore(self.config.push_concurrency)
        self._async_loop = None
        self.notification_failures = []
        self.notification_failure_count = 0

    def connect(self, token, device_id: str) -> Session:
        user = self.auth.authenticate(token)
        if (
            not isinstance(device_id, str)
            or not device_id.strip()
            or len(device_id) > 128
        ):
            raise ChatError("invalid device")
        self._expire_user(user)
        was_online = bool(self._user_sessions.get(user))
        previous = self._sessions.get((user, device_id))
        if previous is not None:
            self._close(previous)
        session = Session(
            user,
            device_id,
            token,
            asyncio.Queue(self.config.session_queue_size),
            self.clock(),
        )
        self._sessions[(user, device_id)] = session
        self._user_sessions.setdefault(user, set()).add(session)
        if not was_online:
            self._publish_presence(user, True)
        return session

    def validate_session(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise AuthenticationError("invalid session")
        self._expire_user(session.user_id)
        if (
            session.closed
            or self._sessions.get((session.user_id, session.device_id)) is not session
        ):
            raise AuthenticationError("session closed")
        if self.auth.authenticate(session.token) != session.user_id:
            self.disconnect(session)
            raise AuthenticationError("invalid session")

    def heartbeat(self, session: Session) -> None:
        self.validate_session(session)
        session.last_heartbeat = self.clock()

    def _unsubscribe(self, session: Session) -> None:
        for user in self._subscriptions.pop(session, ()):
            watchers = self._watchers.get(user)
            if watchers is not None:
                watchers.discard(session)
                if not watchers:
                    self._watchers.pop(user, None)

    def _close(self, session: Session) -> None:
        if session.closed:
            return
        session.closed = True
        key = (session.user_id, session.device_id)
        if self._sessions.get(key) is session:
            self._sessions.pop(key)
        devices = self._user_sessions.get(session.user_id)
        if devices is not None:
            devices.discard(session)
            if not devices:
                self._user_sessions.pop(session.user_id, None)
        self._unsubscribe(session)
        while not session.queue.empty():
            session.queue.get_nowait()
        session.queue.put_nowait({"type": "closed", "reason": "reconnect and sync"})

    def disconnect(self, session: Session) -> None:
        if self._sessions.get((session.user_id, session.device_id)) is session:
            self._close(session)
            if not self._has_live_session(session.user_id):
                self._publish_presence(session.user_id, False)

    def _has_live_session(self, user_id: str) -> bool:
        now = self.clock()
        return any(
            now - s.last_heartbeat < self.config.heartbeat_timeout
            for s in self._user_sessions.get(user_id, ())
        )

    def _expire_user(self, user_id: str) -> None:
        changed = False
        now = self.clock()
        for session in list(self._user_sessions.get(user_id, ())):
            try:
                valid = self.auth.authenticate(session.token) == user_id
            except AuthenticationError:
                valid = False
            if (
                not valid
                or now - session.last_heartbeat >= self.config.heartbeat_timeout
            ):
                self._close(session)
                changed = True
        if changed and not self._has_live_session(user_id):
            self._publish_presence(user_id, False)

    def expire_sessions(self) -> None:
        """Periodic global sweep; request validation touches only its user."""
        for user in list(self._user_sessions):
            self._expire_user(user)

    def is_online(self, user_id: str) -> bool:
        self._expire_user(user_id)
        return self._has_live_session(user_id)

    @staticmethod
    def _user_ids(values):
        if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 100:
            raise ChatError("invalid user list")
        if not all(
            isinstance(value, str) and value.strip() and len(value) <= 128
            for value in values
        ):
            raise ChatError("invalid user list")
        return set(values)

    def subscribe_presence(self, session: Session, user_ids) -> dict[str, bool]:
        self.validate_session(session)
        users = self._user_ids(user_ids)
        self._unsubscribe(session)
        self._subscriptions[session] = users
        for user in users:
            self._watchers.setdefault(user, set()).add(session)
        return {user: self.is_online(user) for user in users}

    def _publish_presence(self, user: str, online: bool) -> None:
        # Queue overflow may close another subscriber and publish its state.
        # A worklist prevents recursive fanout chains from overflowing the stack.
        self._presence_events.append((user, online))
        if self._publishing_presence:
            return
        self._publishing_presence = True
        try:
            while self._presence_events:
                target, state = self._presence_events.popleft()
                for session in list(self._watchers.get(target, ())):
                    self._expire_user(session.user_id)
                    self._enqueue(
                        session,
                        {"type": "presence", "user_id": target, "online": state},
                    )
        finally:
            self._publishing_presence = False

    def _enqueue(self, session: Session, event: dict) -> None:
        if session.closed:
            return
        try:
            session.queue.put_nowait(copy.deepcopy(event))
        except asyncio.QueueFull:
            self.disconnect(session)

    def create_direct(self, session, recipient):
        self.validate_session(session)
        validate_user_id(recipient)
        return self.store.create_channel(session.user_id, [recipient], "direct", 2)

    def create_group(self, session, members):
        self.validate_session(session)
        return self.store.create_channel(
            session.user_id,
            self._user_ids(members),
            "group",
            self.config.max_group_members,
        )

    def add_member(self, session, channel_id, user_id):
        self.validate_session(session)
        validate_user_id(user_id)
        return self.store.add_member(
            channel_id, session.user_id, user_id, self.config.max_group_members
        )

    def _prepare_loop(self):
        loop = asyncio.get_running_loop()
        if loop is self._async_loop:
            return
        if self._async_loop is not None and (
            self._async_loop.is_running() or self._send_lock.locked()
        ):
            raise RuntimeError("ChatService belongs to one active event loop")
        # Preserve the synchronous-core convention of sequential asyncio.run
        # calls. Live transport consumers must stay on their original loop.
        self._send_lock = asyncio.Lock()
        self._push_slots = asyncio.Semaphore(self.config.push_concurrency)
        self._async_loop = loop

    async def _store_call(self, session, operation, *args):
        self._prepare_loop()
        self.validate_session(session)
        result = await run_blocking(operation, *args)
        self.validate_session(session)
        return result

    async def async_create_direct(self, session, recipient):
        validate_user_id(recipient)
        return await self._store_call(
            session,
            self.store.create_channel,
            session.user_id,
            [recipient],
            "direct",
            2,
        )

    async def async_create_group(self, session, members):
        users = self._user_ids(members)
        return await self._store_call(
            session,
            self.store.create_channel,
            session.user_id,
            users,
            "group",
            self.config.max_group_members,
        )

    async def async_add_member(self, session, channel_id, user_id):
        validate_user_id(user_id)
        return await self._store_call(
            session,
            self.store.add_member,
            channel_id,
            session.user_id,
            user_id,
            self.config.max_group_members,
        )

    async def async_sync(self, session, after=0, limit=100):
        read = getattr(self.store, "sync_bounded", self.store.sync)
        return await self._store_call(session, read, session.user_id, after, limit)

    async def async_history(self, session, channel_id, after=0, limit=100):
        read = getattr(self.store, "history_bounded", self.store.history)
        return await self._store_call(
            session, read, channel_id, session.user_id, after, limit
        )

    def get_message(self, session, message_id):
        self.validate_session(session)
        return self.store.get_message(message_id, session.user_id)

    async def async_get_message(self, session, message_id):
        return await self._store_call(
            session, self.store.get_message, message_id, session.user_id
        )

    async def send(self, session, channel_id, content, client_message_id) -> Message:
        self._prepare_loop()
        self.validate_session(session)
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self.config.max_message_length
        ):
            raise ChatError("invalid message content")
        if (
            not isinstance(client_message_id, str)
            or not 1 <= len(client_message_id) <= 200
        ):
            raise ChatError("invalid client message ID")
        # Commit and local fanout are ordered together, independently of push IO.
        async with self._send_lock:
            self.validate_session(session)
            message, created, members = await run_blocking(
                self.store.append,
                channel_id,
                session.user_id,
                content,
                client_message_id,
            )
            self.validate_session(session)
            if not created:
                return message
            offline = self._deliver_live(message, members, session.user_id)
        await self._notify_offline(offline, message)
        self._validate_acknowledgement(session)
        return message

    def _validate_acknowledgement(self, session):
        # Backpressure may close the sender's live queue after the commit.
        # Its accepted send remains valid, while revoked/replaced identities do not.
        try:
            user = self.auth.authenticate(session.token)
        except AuthenticationError:
            self.disconnect(session)
            raise
        current = self._sessions.get((session.user_id, session.device_id))
        if user != session.user_id or (current is not None and current is not session):
            raise AuthenticationError("session replaced")

    def _deliver_live(self, message: Message, members, sender: str) -> list[str]:
        event = {"type": "message", "message": dataclasses.asdict(message)}
        offline = []
        for user in members:
            self._expire_user(user)
            for target in list(self._user_sessions.get(user, ())):
                self._enqueue(target, event)
            if user != sender and not self._has_live_session(user):
                offline.append(user)
        return offline

    async def _notify_offline(self, users: list[str], message: Message) -> None:
        self._prepare_loop()
        recipients = iter(users)

        async def invoke(user):
            async with self._push_slots:
                await self.notifier.notify(user, message)

        async def deliver():
            for user in recipients:
                if self.is_online(user):
                    continue
                try:
                    await asyncio.wait_for(invoke(user), self.config.push_timeout)
                except Exception as error:
                    logger.exception("Offline notification failed")
                    self.notification_failure_count += 1
                    self.notification_failures.append(
                        (user, message.message_id, type(error).__name__)
                    )
                    if len(self.notification_failures) > self.config.failure_retention:
                        del self.notification_failures[0]

        await gather_owned(
            *(deliver() for _ in range(min(self.config.push_concurrency, len(users))))
        )

    def sync(self, session, after=0, limit=100):
        self.validate_session(session)
        return self.store.sync(session.user_id, after, limit)

    def history(self, session, channel_id, after=0, limit=100):
        self.validate_session(session)
        return self.store.history(channel_id, session.user_id, after, limit)
