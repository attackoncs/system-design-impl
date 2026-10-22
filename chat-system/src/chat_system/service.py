"""Single-event-loop chat orchestration, authenticated sessions and
presence.
"""

import asyncio
import dataclasses
import hmac
import logging
import secrets
import time
from typing import Callable, Dict

import chat_system.models
import chat_system.store


class TokenAuthenticator:
    """Provision tokens out of band; do not expose token issuance as a login
    API.
    """

    def __init__(self):
        self._tokens = {}

    def issue(self, user_id):
        """Issue a random bearer token for a nonempty user identifier."""
        if not isinstance(user_id, str) or not user_id:
            raise chat_system.models.ChatError("invalid user")
        token = secrets.token_urlsafe(32)
        self._tokens[token] = user_id
        return token

    def authenticate(self, token):
        """Return the token owner, or raise AuthenticationError if invalid."""
        if (
            not isinstance(token, str)
            or not token.isascii()
            or len(token) > 256
        ):
            raise chat_system.models.AuthenticationError("invalid token")
        for known, user in self._tokens.items():
            if hmac.compare_digest(token, known):
                return user
        raise chat_system.models.AuthenticationError("invalid token")

    def revoke(self, token):
        """Revoke a token; unknown tokens are ignored."""
        self._tokens.pop(token, None)


@dataclasses.dataclass(eq=False)
class Session:
    """A device connection with a bounded event queue and heartbeat state."""

    user_id: str
    device_id: str
    token: str = dataclasses.field(repr=False)
    queue: asyncio.Queue = dataclasses.field(repr=False)
    last_heartbeat: float = 0.0
    closed: bool = False


class RecordingNotifier:
    """Reference push adapter.

    Replace with a provider for real delivery.
    """

    def __init__(self):
        self.notifications = []

    async def notify(self, user_id, message):
        """Record a (user_id, message) pair for the reference push adapter."""
        self.notifications.append((user_id, message))


class ChatService:
    """Single-event-loop chat sessions, presence, persistence, and delivery."""

    def __init__(
        self,
        store: chat_system.store.MessageStore = None,
        authenticator=None,
        notifier=None,
        config=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.store = (
            store
            if store is not None
            else chat_system.store.SQLiteMessageStore()
        )
        self.auth = (
            authenticator if authenticator is not None else TokenAuthenticator()
        )
        self.notifier = (
            notifier if notifier is not None else RecordingNotifier()
        )
        self.config = config or chat_system.models.ChatConfig()
        self.clock = clock
        self._sessions: Dict[tuple, Session] = {}
        self._subscriptions = {}
        self.notification_failures = []

    def connect(self, token, device_id):
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
        user = self.auth.authenticate(token)
        if not isinstance(device_id, str) or not device_id:
            raise chat_system.models.ChatError("invalid device")
        self.expire_sessions()
        was_online = self.is_online(user)
        previous = self._sessions.get((user, device_id))
        if previous:
            self._close(previous)
        session = Session(
            user,
            device_id,
            token,
            asyncio.Queue(self.config.session_queue_size),
            self.clock(),
        )
        self._sessions[(user, device_id)] = session
        if not was_online:
            self._publish_presence(user, True)
        return session

    def validate_session(self, session):
        self.expire_sessions()
        if (
            session.closed
            or self._sessions.get((session.user_id, session.device_id))
            is not session
        ):
            raise chat_system.models.AuthenticationError("session closed")
        if self.auth.authenticate(session.token) != session.user_id:
            raise chat_system.models.AuthenticationError("invalid session")

    def heartbeat(self, session):
        """Refresh the lease of an authenticated active session.

        Args:
            session: Session returned by connect.

        Raises:
            AuthenticationError: The session has expired or been replaced.
        """
        self.validate_session(session)
        session.last_heartbeat = self.clock()

    def _close(self, session):
        session.closed = True
        self._sessions.pop((session.user_id, session.device_id), None)
        self._subscriptions.pop(session, None)
        while not session.queue.empty():
            session.queue.get_nowait()
        session.queue.put_nowait(
            {"type": "closed", "reason": "reconnect and sync"}
        )

    def disconnect(self, session):
        """Close a session and publish offline presence when appropriate.

        Args:
            session: Session to close; repeated disconnects are harmless.
        """
        if self._sessions.get((session.user_id, session.device_id)) is session:
            self._close(session)
            if not self.is_online(session.user_id):
                self._publish_presence(session.user_id, False)

    def expire_sessions(self):
        """Disconnect expired or revoked sessions and update presence."""
        now = self.clock()
        for session in list(self._sessions.values()):
            try:
                valid = self.auth.authenticate(session.token) == session.user_id
            except chat_system.models.AuthenticationError:
                valid = False
            if (
                not valid
                or now - session.last_heartbeat >= self.config.heartbeat_timeout
            ):
                self.disconnect(session)

    def is_online(self, user_id):
        """Return whether user_id currently has a live device session."""
        now = self.clock()
        return any(
            s.user_id == user_id
            and now - s.last_heartbeat < self.config.heartbeat_timeout
            for s in self._sessions.values()
        )

    def subscribe_presence(self, session, user_ids):
        """Replace a session's subscriptions and return current online states.

        Args:
            session: Authenticated session receiving presence events.
            user_ids: At most 100 nonempty user identifiers.

        Returns:
            A mapping from subscribed user IDs to online booleans.

        Raises:
            ChatError: The subscription or session is invalid.
        """
        self.validate_session(session)
        users = set(user_ids)
        if len(users) > 100 or not all(isinstance(u, str) and u for u in users):
            raise chat_system.models.ChatError("invalid presence subscription")
        self._subscriptions[session] = users
        return {u: self.is_online(u) for u in users}

    def _publish_presence(self, user, online):
        for session, users in list(self._subscriptions.items()):
            if user in users:
                self._enqueue(
                    session,
                    {"type": "presence", "user_id": user, "online": online},
                )

    def _enqueue(self, session, event):
        if session.closed:
            return
        try:
            session.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Durable inbox is authoritative; disconnect slow clients instead of
            # dropping silently.
            self.disconnect(session)

    def create_direct(self, session, recipient):
        """Return the direct channel between the session user and recipient.

        Args:
            session: Authenticated caller session.
            recipient: The other user's identifier.

        Returns:
            An existing or newly created two-member Channel.
        """
        self.validate_session(session)
        return self.store.create_channel(
            session.user_id, [recipient], "direct", 2
        )

    def create_group(self, session, members):
        """Create a group owned by the authenticated session user.

        Args:
            session: Authenticated owner session.
            members: Initial member IDs; the owner is included automatically.

        Returns:
            The new Channel.

        Raises:
            ChatError: The member list violates the configured group limit.
        """
        self.validate_session(session)
        return self.store.create_channel(
            session.user_id, members, "group", self.config.max_group_members
        )

    def add_member(self, session, channel_id, user_id):
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
        self.validate_session(session)
        return self.store.add_member(
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
        self.validate_session(session)
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) > self.config.max_message_length
        ):
            raise chat_system.models.ChatError("invalid message content")
        if (
            not isinstance(client_message_id, str)
            or not 1 <= len(client_message_id) <= 200
        ):
            raise chat_system.models.ChatError("invalid client message ID")
        message, created, members = self.store.append(
            channel_id, session.user_id, content, client_message_id
        )
        if not created:
            return message
        offline = []
        for user in members:
            for target in list(self._sessions.values()):
                if target.user_id == user:
                    self._enqueue(
                        target,
                        {
                            "type": "message",
                            "message": dataclasses.asdict(message),
                        },
                    )
            if user != session.user_id and not self.is_online(user):
                offline.append(user)
        # Enqueue every live recipient before awaiting a provider to preserve
        # live ID order.
        for user in offline:
            try:
                await self.notifier.notify(user, message)
            # Isolate this operation; record failures before continuing.
            except Exception as error:  # pylint: disable=broad-exception-caught
                # Persistence and sender acknowledgement must survive push
                # provider failure.
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "send"
                )
                self.notification_failures.append(
                    (user, message.message_id, type(error).__name__)
                )
        return message

    def sync(self, session, after=0, limit=100):
        """Return an ascending page of the session user's durable inbox.

        Args:
            session: Authenticated device session.
            after: Exclusive message ID cursor; zero starts from the beginning.
            limit: Maximum number of messages to return.

        Returns:
            Messages after the cursor, ordered by message ID.
        """
        self.validate_session(session)
        return self.store.sync(session.user_id, after, limit)

    def history(self, session, channel_id, after=0, limit=100):
        """Return an ascending page of an accessible channel's messages.

        Args:
            session: Authenticated channel member session.
            channel_id: Channel whose history should be read.
            after: Exclusive message ID cursor.
            limit: Maximum number of messages to return.

        Returns:
            Messages visible to the caller after the cursor.
        """
        self.validate_session(session)
        return self.store.history(channel_id, session.user_id, after, limit)
