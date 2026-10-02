"""Single-event-loop chat orchestration, authenticated sessions and presence."""
import asyncio
import hmac
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict

from .models import AuthenticationError, ChatConfig, ChatError
from .store import MessageStore, SQLiteMessageStore


class TokenAuthenticator:
    """Provision tokens out of band; do not expose token issuance as a login API."""

    def __init__(self):
        self._tokens = {}

    def issue(self, user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ChatError("invalid user")
        token = secrets.token_urlsafe(32)
        self._tokens[token] = user_id
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or not token.isascii() or len(token) > 256:
            raise AuthenticationError("invalid token")
        for known, user in self._tokens.items():
            if hmac.compare_digest(token, known):
                return user
        raise AuthenticationError("invalid token")

    def revoke(self, token):
        self._tokens.pop(token, None)


@dataclass(eq=False)
class Session:
    user_id: str
    device_id: str
    token: str = field(repr=False)
    queue: asyncio.Queue = field(repr=False)
    last_heartbeat: float = 0.0
    closed: bool = False


class RecordingNotifier:
    """Reference push adapter. Replace with a provider for real delivery."""

    def __init__(self):
        self.notifications = []

    async def notify(self, user_id, message):
        self.notifications.append((user_id, message))


class ChatService:
    def __init__(self, store: MessageStore = None, authenticator=None, notifier=None,
                 config=None, clock: Callable[[], float] = time.monotonic):
        self.store = store if store is not None else SQLiteMessageStore()
        self.auth = authenticator if authenticator is not None else TokenAuthenticator()
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.config = config or ChatConfig()
        self.clock = clock
        self._sessions: Dict[tuple, Session] = {}
        self._subscriptions = {}
        self.notification_failures = []

    def connect(self, token, device_id):
        user = self.auth.authenticate(token)
        if not isinstance(device_id, str) or not device_id:
            raise ChatError("invalid device")
        self.expire_sessions()
        was_online = self.is_online(user)
        previous = self._sessions.get((user, device_id))
        if previous:
            self._close(previous)
        session = Session(user, device_id, token,
                          asyncio.Queue(self.config.session_queue_size), self.clock())
        self._sessions[(user, device_id)] = session
        if not was_online:
            self._publish_presence(user, True)
        return session

    def _require(self, session):
        self.expire_sessions()
        if session.closed or self._sessions.get((session.user_id, session.device_id)) is not session:
            raise AuthenticationError("session closed")
        if self.auth.authenticate(session.token) != session.user_id:
            raise AuthenticationError("invalid session")

    def heartbeat(self, session):
        self._require(session)
        session.last_heartbeat = self.clock()

    def _close(self, session):
        session.closed = True
        self._sessions.pop((session.user_id, session.device_id), None)
        self._subscriptions.pop(session, None)
        while not session.queue.empty():
            session.queue.get_nowait()
        session.queue.put_nowait({"type": "closed", "reason": "reconnect and sync"})

    def disconnect(self, session):
        if self._sessions.get((session.user_id, session.device_id)) is session:
            self._close(session)
            if not self.is_online(session.user_id):
                self._publish_presence(session.user_id, False)

    def expire_sessions(self):
        now = self.clock()
        for session in list(self._sessions.values()):
            try:
                valid = self.auth.authenticate(session.token) == session.user_id
            except AuthenticationError:
                valid = False
            if not valid or now - session.last_heartbeat >= self.config.heartbeat_timeout:
                self.disconnect(session)

    def is_online(self, user_id):
        now = self.clock()
        return any(s.user_id == user_id and now - s.last_heartbeat < self.config.heartbeat_timeout
                   for s in self._sessions.values())

    def subscribe_presence(self, session, user_ids):
        self._require(session)
        users = set(user_ids)
        if len(users) > 100 or not all(isinstance(u, str) and u for u in users):
            raise ChatError("invalid presence subscription")
        self._subscriptions[session] = users
        return {u: self.is_online(u) for u in users}

    def _publish_presence(self, user, online):
        for session, users in list(self._subscriptions.items()):
            if user in users:
                self._enqueue(session, {"type": "presence", "user_id": user, "online": online})

    def _enqueue(self, session, event):
        if session.closed:
            return
        try:
            session.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Durable inbox is authoritative; disconnect slow clients instead of dropping silently.
            self.disconnect(session)

    def create_direct(self, session, recipient):
        self._require(session)
        return self.store.create_channel(session.user_id, [recipient], "direct", 2)

    def create_group(self, session, members):
        self._require(session)
        return self.store.create_channel(session.user_id, members, "group", self.config.max_group_members)

    def add_member(self, session, channel_id, user_id):
        self._require(session)
        return self.store.add_member(channel_id, session.user_id, user_id, self.config.max_group_members)

    async def send(self, session, channel_id, content, client_message_id):
        self._require(session)
        if not isinstance(content, str) or not content.strip() or len(content) > self.config.max_message_length:
            raise ChatError("invalid message content")
        if not isinstance(client_message_id, str) or not 1 <= len(client_message_id) <= 200:
            raise ChatError("invalid client message ID")
        message, created, members = self.store.append(channel_id, session.user_id, content, client_message_id)
        if not created:
            return message
        offline = []
        for user in members:
            for target in list(self._sessions.values()):
                if target.user_id == user:
                    self._enqueue(target, {"type": "message", "message": asdict(message)})
            if user != session.user_id and not self.is_online(user):
                offline.append(user)
        # Enqueue every live recipient before awaiting a provider to preserve live ID order.
        for user in offline:
            try:
                await self.notifier.notify(user, message)
            except Exception as error:
                # Persistence and sender acknowledgement must survive push provider failure.
                self.notification_failures.append((user, message.message_id, type(error).__name__))
        return message

    def sync(self, session, after=0, limit=100):
        self._require(session)
        return self.store.sync(session.user_id, after, limit)

    def history(self, session, channel_id, after=0, limit=100):
        self._require(session)
        return self.store.history(channel_id, session.user_id, after, limit)
