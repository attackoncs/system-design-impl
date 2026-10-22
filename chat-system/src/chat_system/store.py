"""SQLite reference store: atomic persistence and per-recipient inbox
fanout.
"""

import sqlite3
import time
import uuid
from typing import Protocol

import chat_system.models


class MessageStore(Protocol):
    """Persistence contract for channels, idempotent messages, and ordered
    inboxes.
    """

    def get_channel(self, channel_id: str) -> chat_system.models.Channel: ...
    def create_channel(
        self, owner_id, members, kind, limit
    ) -> chat_system.models.Channel: ...
    def add_member(self, channel_id, actor, user_id, limit): ...
    def append(self, channel_id, sender_id, content, client_message_id): ...
    def sync(self, user_id, after=0, limit=100): ...
    def history(self, channel_id, user_id, after=0, limit=100): ...


class SQLiteMessageStore:
    """Use one instance per service; operations run synchronously in its event
    loop.
    """

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            "\n"
            "            CREATE TABLE IF NOT EXISTS channels (\n"
            "                id TEXT PRIMARY KEY, owner TEXT NOT NULL, kin"
            "d TEXT NOT NULL);\n"
            "            CREATE TABLE IF NOT EXISTS members (\n"
            "                channel TEXT REFERENCES channels(id), user TE"
            "XT NOT NULL,\n"
            "                PRIMARY KEY(channel, user));\n"
            "            CREATE TABLE IF NOT EXISTS messages (\n"
            "                id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            "                channel TEXT NOT NULL REFERENCES channels(id)"
            ", sender TEXT NOT NULL,\n"
            "                content TEXT NOT NULL, created REAL NOT NULL,"
            " client_id TEXT NOT NULL,\n"
            "                UNIQUE(sender, client_id));\n"
            "            CREATE INDEX IF NOT EXISTS message_channel ON mes"
            "sages(channel, id);\n"
            "            CREATE TABLE IF NOT EXISTS inbox (\n"
            "                user TEXT NOT NULL, message INTEGER REFERENCE"
            "S messages(id),\n"
            "                PRIMARY KEY(user, message));\n"
            "        "
        )

    def close(self):
        self.db.close()

    def get_channel(self, channel_id):
        row = self.db.execute(
            "SELECT owner, kind FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        if row is None:
            raise chat_system.models.ChatError("channel not found")
        members = tuple(
            r[0]
            for r in self.db.execute(
                "SELECT user FROM members WHERE channel=? ORDER BY user",
                (channel_id,),
            )
        )
        return chat_system.models.Channel(channel_id, row[0], row[1], members)

    def create_channel(self, owner_id, members, kind, limit):
        users = set(members) | {owner_id}
        if kind not in ("direct", "group") or not all(
            isinstance(u, str) and u for u in users
        ):
            raise chat_system.models.ChatError("invalid channel")
        if (
            len(users) < 2
            or len(users) > limit
            or (kind == "direct" and len(users) != 2)
        ):
            raise chat_system.models.ChatError("invalid member count")
        if kind == "direct":
            for (existing,) in self.db.execute(
                "SELECT id FROM channels WHERE kind='direct'"
            ):
                if set(self.get_channel(existing).members) == users:
                    return self.get_channel(existing)
        channel_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO channels VALUES (?, ?, ?)",
                (channel_id, owner_id, kind),
            )
            self.db.executemany(
                "INSERT INTO members VALUES (?, ?)",
                [(channel_id, u) for u in users],
            )
        return self.get_channel(channel_id)

    def add_member(self, channel_id, actor, user_id, limit):
        channel = self.get_channel(channel_id)
        if channel.owner_id != actor or channel.kind != "group":
            raise chat_system.models.ChatError(
                "only group owner may add members"
            )
        if not isinstance(user_id, str) or not user_id:
            raise chat_system.models.ChatError("invalid user")
        if user_id in channel.members:
            return channel
        if len(channel.members) >= limit:
            raise chat_system.models.ChatError("group is full")
        with self.db:
            self.db.execute(
                "INSERT INTO members VALUES (?, ?)", (channel_id, user_id)
            )
        return self.get_channel(channel_id)

    def append(self, channel_id, sender_id, content, client_message_id):
        channel = self.get_channel(channel_id)
        if sender_id not in channel.members:
            raise chat_system.models.ChatError("not a channel member")
        row = self.db.execute(
            "SELECT * FROM messages WHERE sender=? AND client_id=?",
            (sender_id, client_message_id),
        ).fetchone()
        if row:
            message = chat_system.models.Message(*row)
            if message.channel_id != channel_id or message.content != content:
                raise chat_system.models.ChatError(
                    "idempotency key already used for a different message"
                )
            return message, False, channel.members
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO messages(channel,sender,content,created,client_id)"
                " VALUES (?,?,?,?,?)",
                (
                    channel_id,
                    sender_id,
                    content,
                    time.time(),
                    client_message_id,
                ),
            )
            message_id = cursor.lastrowid
            self.db.executemany(
                "INSERT INTO inbox VALUES (?, ?)",
                [(u, message_id) for u in channel.members],
            )
        row = self.db.execute(
            "SELECT * FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        return chat_system.models.Message(*row), True, channel.members

    @staticmethod
    def _page(after, limit):
        if (
            not isinstance(after, int)
            or after < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise chat_system.models.ChatError("invalid cursor or page size")

    def sync(self, user_id, after=0, limit=100):
        self._page(after, limit)
        rows = self.db.execute(
            "SELECT m.* FROM inbox i JOIN messages m ON m.id=i.message "
            "WHERE i.user=? AND m.id>? ORDER BY m.id LIMIT ?",
            (user_id, after, limit),
        )
        return [chat_system.models.Message(*row) for row in rows]

    def history(self, channel_id, user_id, after=0, limit=100):
        self._page(after, limit)
        if user_id not in self.get_channel(channel_id).members:
            raise chat_system.models.ChatError("not a channel member")
        # Newly added members cannot see messages sent before they joined.
        rows = self.db.execute(
            "SELECT m.* FROM messages m JOIN inbox i ON i.message=m.id "
            "WHERE m.channel=? AND i.user=? AND m.id>? ORDER BY m.id LIMIT ?",
            (channel_id, user_id, after, limit),
        )
        return [chat_system.models.Message(*row) for row in rows]
