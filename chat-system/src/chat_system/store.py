"""Transactional SQLite channels, idempotent messages, and recipient inboxes."""

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from typing import Iterable, Protocol

from chat_system.models import Channel, ChatError, Message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    channel TEXT REFERENCES channels(id),
    user TEXT NOT NULL,
    PRIMARY KEY (channel, user)
);
CREATE TABLE IF NOT EXISTS direct_pairs (
    first_user TEXT NOT NULL,
    second_user TEXT NOT NULL,
    channel TEXT NOT NULL UNIQUE REFERENCES channels(id),
    PRIMARY KEY (first_user, second_user)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL REFERENCES channels(id),
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    created REAL NOT NULL,
    client_id TEXT NOT NULL,
    UNIQUE (sender, client_id)
);
CREATE INDEX IF NOT EXISTS message_channel ON messages(channel, id);
CREATE TABLE IF NOT EXISTS inbox (
    user TEXT NOT NULL,
    message INTEGER REFERENCES messages(id),
    PRIMARY KEY (user, message)
);
"""


class MessageStore(Protocol):
    """Synchronous persistence contract; callers may offload it to a thread."""

    def get_channel(self, channel_id: str) -> Channel: ...

    def create_channel(
        self, owner_id: str, members: Iterable[str], kind: str, limit: int
    ) -> Channel: ...

    def add_member(
        self, channel_id: str, actor: str, user_id: str, limit: int
    ) -> Channel: ...

    def append(
        self, channel_id: str, sender_id: str, content: str, client_message_id: str
    ) -> tuple[Message, bool, tuple[str, ...]]: ...

    def sync(self, user_id: str, after: int = 0, limit: int = 100) -> list[Message]: ...

    def history(
        self, channel_id: str, user_id: str, after: int = 0, limit: int = 100
    ) -> list[Message]: ...

    def get_message(self, message_id: int, user_id: str) -> Message: ...


class SQLiteMessageStore:
    """Thread-safe local store with atomic membership and message fanout.

    A lock serializes operations sharing this connection, including ``:memory:``
    databases. ``BEGIN IMMEDIATE`` also serializes writers in separate store
    instances/processes that share a file. File databases use WAL and fully
    synchronous commits. The synchronous API can run through ``asyncio.to_thread``.

    ``db`` remains available for diagnostics; direct SQL callers must coordinate
    access themselves and must not leave transactions open around store calls.
    """

    def __init__(self, path=":memory:"):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("PRAGMA synchronous = FULL")
            self.db.executescript(_SCHEMA)
            self._index_existing_direct_channels()
        except BaseException:
            self.db.close()
            raise

    @contextlib.contextmanager
    def _transaction(self, *, write=False):
        """Keep read snapshots coherent and reserve writes before validation."""
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _index_existing_direct_channels(self):
        """Backfill the pair index for channels created by older versions.

        Legacy duplicate channels keep their history and IDs; the first existing
        pair becomes the canonical channel returned by future create requests.
        """
        with self._transaction(write=True):
            channels = self.db.execute(
                "SELECT c.id FROM channels c "
                "LEFT JOIN direct_pairs d ON d.channel=c.id "
                "WHERE c.kind='direct' AND d.channel IS NULL ORDER BY c.rowid"
            ).fetchall()
            for (channel_id,) in channels:
                members = self._get_channel(channel_id).members
                if len(members) == 2:
                    self.db.execute(
                        "INSERT OR IGNORE INTO direct_pairs "
                        "(first_user, second_user, channel) VALUES (?, ?, ?)",
                        (*members, channel_id),
                    )

    def close(self):
        """Close the connection after any active operation finishes."""
        with self._lock:
            self.db.close()

    @staticmethod
    def _identifier(value, label):
        if not isinstance(value, str) or not value:
            raise ChatError(f"invalid {label}")

    @staticmethod
    def _group_limit(limit):
        if type(limit) is not int or not 2 <= limit <= 100:
            raise ChatError("group limit must be between 2 and 100")

    def _get_channel(self, channel_id):
        row = self.db.execute(
            "SELECT owner, kind FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if row is None:
            raise ChatError("channel not found")
        members = tuple(
            item[0]
            for item in self.db.execute(
                "SELECT user FROM members WHERE channel=? ORDER BY user",
                (channel_id,),
            )
        )
        return Channel(channel_id, row[0], row[1], members)

    def get_channel(self, channel_id):
        """Return channel details and membership from one read snapshot."""
        self._identifier(channel_id, "channel")
        with self._transaction():
            return self._get_channel(channel_id)

    def create_channel(self, owner_id, members, kind, limit):
        """Create a group, or atomically reuse the indexed direct-user pair."""
        self._identifier(owner_id, "owner")
        self._group_limit(limit)
        if kind not in ("direct", "group") or isinstance(members, (str, bytes)):
            raise ChatError("invalid channel")
        users = {owner_id}
        try:
            for user in members:
                self._identifier(user, "user")
                users.add(user)
                if len(users) > limit:
                    raise ChatError("invalid member count")
        except TypeError as error:
            raise ChatError("invalid channel members") from error
        if len(users) < 2 or (kind == "direct" and len(users) != 2):
            raise ChatError("invalid member count")
        ordered_users = tuple(sorted(users))
        with self._transaction(write=True):
            if kind == "direct":
                row = self.db.execute(
                    "SELECT channel FROM direct_pairs "
                    "WHERE first_user=? AND second_user=?",
                    ordered_users,
                ).fetchone()
                if row is not None:
                    return self._get_channel(row[0])
            channel_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO channels (id, owner, kind) VALUES (?, ?, ?)",
                (channel_id, owner_id, kind),
            )
            self.db.executemany(
                "INSERT INTO members (channel, user) VALUES (?, ?)",
                [(channel_id, user) for user in ordered_users],
            )
            if kind == "direct":
                self.db.execute(
                    "INSERT INTO direct_pairs (first_user, second_user, channel) "
                    "VALUES (?, ?, ?)",
                    (*ordered_users, channel_id),
                )
            return self._get_channel(channel_id)

    def add_member(self, channel_id, actor, user_id, limit):
        """Check authorization/capacity and insert membership in one transaction."""
        self._identifier(channel_id, "channel")
        self._identifier(actor, "actor")
        self._identifier(user_id, "user")
        self._group_limit(limit)
        with self._transaction(write=True):
            channel = self._get_channel(channel_id)
            if channel.owner_id != actor or channel.kind != "group":
                raise ChatError("only group owner may add members")
            if user_id in channel.members:
                return channel
            if len(channel.members) >= limit:
                raise ChatError("group is full")
            self.db.execute(
                "INSERT INTO members (channel, user) VALUES (?, ?)",
                (channel_id, user_id),
            )
            return self._get_channel(channel_id)

    def append(self, channel_id, sender_id, content, client_message_id):
        """Commit one sender-scoped retry key, message, and recipient snapshot."""
        self._identifier(channel_id, "channel")
        self._identifier(sender_id, "sender")
        if (
            not isinstance(content, str)
            or not content.strip()
            or len(content) >= 100_000
        ):
            raise ChatError("invalid message content")
        if (
            not isinstance(client_message_id, str)
            or not 1 <= len(client_message_id) <= 200
        ):
            raise ChatError("invalid client message ID")
        with self._transaction(write=True):
            channel = self._get_channel(channel_id)
            if sender_id not in channel.members:
                raise ChatError("not a channel member")
            row = self.db.execute(
                "SELECT id, channel, sender, content, created, client_id "
                "FROM messages WHERE sender=? AND client_id=?",
                (sender_id, client_message_id),
            ).fetchone()
            if row is not None:
                message = Message(*row)
                if message.channel_id != channel_id or message.content != content:
                    raise ChatError(
                        "idempotency key already used for a different message"
                    )
                return message, False, channel.members
            created_at = time.time()
            cursor = self.db.execute(
                "INSERT INTO messages (channel, sender, content, created, client_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_id, sender_id, content, created_at, client_message_id),
            )
            message_id = cursor.lastrowid
            self.db.executemany(
                "INSERT INTO inbox (user, message) VALUES (?, ?)",
                [(user, message_id) for user in channel.members],
            )
            message = Message(
                message_id,
                channel_id,
                sender_id,
                content,
                created_at,
                client_message_id,
            )
            return message, True, channel.members

    @staticmethod
    def _page(after, limit):
        if (
            type(after) is not int
            or not 0 <= after <= 2**63 - 1
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ChatError("invalid cursor or page size")

    @staticmethod
    def _read_messages(rows, byte_limit):
        """Iterate pages without first materializing all stored text bodies."""
        if byte_limit is not None and (type(byte_limit) is not int or byte_limit < 1):
            raise ChatError("invalid page byte limit")
        messages = []
        used = 2  # JSON list brackets; transport adds its own envelope budget.
        for row in rows:
            message = Message(*row)
            if byte_limit is not None:
                # Match the transport's UTF-8 JSON encoding, including field
                # names and escaping of control characters.
                payload = {
                    "message_id": message.message_id,
                    "channel_id": message.channel_id,
                    "sender_id": message.sender_id,
                    "content": message.content,
                    "created_at": message.created_at,
                    "client_message_id": message.client_message_id,
                }
                size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 2
                if used + size > byte_limit:
                    if not messages:
                        raise ChatError("message exceeds page byte limit")
                    break
                used += size
            messages.append(message)
        return messages

    def sync(self, user_id, after=0, limit=100):
        """Return a bounded count of the user's inbox in ascending ID order."""
        return self.sync_bounded(user_id, after, limit, byte_limit=None)

    def sync_bounded(self, user_id, after=0, limit=100, *, byte_limit=1_800_000):
        """Read at most limit messages and byte_limit encoded message bytes."""
        self._identifier(user_id, "user")
        self._page(after, limit)
        with self._transaction():
            rows = self.db.execute(
                "SELECT m.id, m.channel, m.sender, m.content, m.created, m.client_id "
                "FROM inbox i JOIN messages m ON m.id=i.message "
                "WHERE i.user=? AND i.message>? ORDER BY i.message LIMIT ?",
                (user_id, after, limit),
            )
            try:
                return self._read_messages(rows, byte_limit)
            finally:
                rows.close()

    def history(self, channel_id, user_id, after=0, limit=100):
        """Return only channel messages that included this user at send time."""
        return self.history_bounded(channel_id, user_id, after, limit, byte_limit=None)

    def get_message(self, message_id, user_id):
        """Read an exact message ID only if it belongs to the user's inbox."""
        self._identifier(user_id, "user")
        if type(message_id) is not int or not 1 <= message_id <= 2**63 - 1:
            raise ChatError("invalid message ID")
        with self._transaction():
            row = self.db.execute(
                "SELECT m.id, m.channel, m.sender, m.content, m.created, m.client_id "
                "FROM messages m JOIN inbox i ON i.message=m.id "
                "WHERE m.id=? AND i.user=?",
                (message_id, user_id),
            ).fetchone()
            if row is None:
                raise ChatError("message not found")
            return Message(*row)

    def history_bounded(
        self, channel_id, user_id, after=0, limit=100, *, byte_limit=1_800_000
    ):
        """Apply both an item limit and a serialized byte budget to history."""
        self._identifier(channel_id, "channel")
        self._identifier(user_id, "user")
        self._page(after, limit)
        with self._transaction():
            if user_id not in self._get_channel(channel_id).members:
                raise ChatError("not a channel member")
            rows = self.db.execute(
                "SELECT m.id, m.channel, m.sender, m.content, m.created, m.client_id "
                "FROM messages m JOIN inbox i ON i.message=m.id "
                "WHERE m.channel=? AND i.user=? AND m.id>? ORDER BY m.id LIMIT ?",
                (channel_id, user_id, after, limit),
            )
            try:
                return self._read_messages(rows, byte_limit)
            finally:
                rows.close()
