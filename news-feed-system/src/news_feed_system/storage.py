"""Post/profile sources of truth, separate from evictable display caches.

SQLite adapters use independent short transactions, WAL and full synchronous
writes. Blocking operations belong in a worker thread when called from asyncio.
The pending flag is a publication outbox: storing a post and requesting fanout
are one transaction. Delivery remains at least once.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional, Protocol, Tuple

from news_feed_system.models import Post, UserProfile

PostKey = Tuple[dt.datetime, str]


def post_key(post: Post) -> PostKey:
    return post.created_at, post.post_id


def snapshot(post: Post) -> Post:
    return dataclasses.replace(
        post,
        media_urls=list(post.media_urls),
        selective_sharing=(
            None if post.selective_sharing is None else list(post.selective_sharing)
        ),
    )


def timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Post timestamps must include a timezone")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def _payload(post: Post) -> str:
    data = dataclasses.asdict(post)
    data["created_at"] = timestamp(post.created_at)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _post(payload: str) -> Post:
    data = json.loads(payload)
    data["created_at"] = dt.datetime.fromisoformat(data["created_at"])
    return Post(**data)


class PostStore(Protocol):
    def put(self, post: Post) -> None: ...
    def get(self, post_id: str) -> Optional[Post]: ...
    def get_by_author(
        self, author_id: str, limit: int = 20, before: Optional[PostKey] = None
    ) -> list[Post]: ...
    def pending_posts(self, limit: int = 100) -> list[Post]: ...
    def mark_dispatched(self, post_id: str) -> None: ...
    def is_pending(self, post_id: str) -> bool: ...
    def defer(self, post_id: str) -> None: ...


class UserStore(Protocol):
    def put(self, profile: UserProfile) -> None: ...
    def get(self, user_id: str) -> Optional[UserProfile]: ...


class InMemoryPostStore:
    """Unbounded development source; process restart loses posts and outbox."""

    def __init__(self) -> None:
        self._posts: dict[str, Post] = {}
        self._authors: dict[str, dict[str, Post]] = {}
        self._pending: dict[str, None] = {}
        self._lock = threading.RLock()

    def put(self, post: Post) -> None:
        timestamp(post.created_at)
        with self._lock:
            existing = self._posts.get(post.post_id)
            if existing is not None:
                if existing != post:
                    raise ValueError("A post ID cannot be overwritten")
                return
            stored = snapshot(post)
            self._posts[post.post_id] = stored
            self._authors.setdefault(post.author_id, {})[post.post_id] = stored
            self._pending[post.post_id] = None

    def get(self, post_id: str) -> Optional[Post]:
        with self._lock:
            post = self._posts.get(post_id)
            return snapshot(post) if post is not None else None

    def get_by_author(
        self, author_id: str, limit: int = 20, before: Optional[PostKey] = None
    ) -> list[Post]:
        _limit(limit)
        with self._lock:
            posts = self._authors.get(author_id, {}).values()
            eligible = [p for p in posts if before is None or post_key(p) < before]
            return [
                snapshot(p)
                for p in sorted(eligible, key=post_key, reverse=True)[:limit]
            ]

    def pending_posts(self, limit: int = 100) -> list[Post]:
        _limit(limit)
        with self._lock:
            return [snapshot(self._posts[p]) for p in list(self._pending)[:limit]]

    def mark_dispatched(self, post_id: str) -> None:
        with self._lock:
            self._pending.pop(post_id, None)

    def is_pending(self, post_id: str) -> bool:
        with self._lock:
            return post_id in self._pending

    def defer(self, post_id: str) -> None:
        with self._lock:
            if post_id in self._pending:
                self._pending.pop(post_id)
                self._pending[post_id] = None


class InMemoryUserStore:
    def __init__(self) -> None:
        self._profiles: dict[str, UserProfile] = {}
        self._lock = threading.RLock()

    def put(self, profile: UserProfile) -> None:
        with self._lock:
            self._profiles[profile.user_id] = profile

    def get(self, user_id: str) -> Optional[UserProfile]:
        with self._lock:
            return self._profiles.get(user_id)


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("limit must be a positive integer")


class SQLiteDatabase:
    """Shared connection policy for posts, profiles and relationship adapters."""

    def __init__(self, path: str) -> None:
        if str(path) == ":memory:":
            raise ValueError("Use a file path for persistent storage")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY, author_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    pending INTEGER NOT NULL DEFAULT 1,
                    last_attempt INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS posts_author_time
                    ON posts(author_id, created_at DESC, post_id DESC);
                CREATE INDEX IF NOT EXISTS posts_pending
                    ON posts(created_at, post_id) WHERE pending = 1;
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS follows (
                    follower_id TEXT NOT NULL, followee_id TEXT NOT NULL,
                    PRIMARY KEY (follower_id, followee_id)
                );
                CREATE INDEX IF NOT EXISTS follows_followee
                    ON follows(followee_id, follower_id);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
            if "last_attempt" not in columns:
                db.execute(
                    "ALTER TABLE posts ADD COLUMN last_attempt INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS pending_fair_order ON posts(last_attempt,created_at,post_id) WHERE pending=1"
            )

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db


class SQLitePostStore:
    def __init__(self, path: str) -> None:
        self._database = SQLiteDatabase(path)

    def put(self, post: Post) -> None:
        payload = _payload(post)
        with self._database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM posts WHERE post_id=?", (post.post_id,)
            ).fetchone()
            if row is not None:
                if row[0] != payload:
                    raise ValueError("A post ID cannot be overwritten")
                return
            db.execute(
                "INSERT INTO posts(post_id,author_id,created_at,payload) VALUES(?,?,?,?)",
                (post.post_id, post.author_id, timestamp(post.created_at), payload),
            )

    def get(self, post_id: str) -> Optional[Post]:
        with self._database.connect() as db:
            row = db.execute(
                "SELECT payload FROM posts WHERE post_id=?", (post_id,)
            ).fetchone()
            return _post(row[0]) if row else None

    def get_by_author(
        self, author_id: str, limit: int = 20, before: Optional[PostKey] = None
    ) -> list[Post]:
        _limit(limit)
        clause = " AND (created_at,post_id) < (?,?)" if before else ""
        args = [author_id]
        if before:
            args.extend([timestamp(before[0]), before[1]])
        args.append(limit)
        with self._database.connect() as db:
            rows = db.execute(
                "SELECT payload FROM posts WHERE author_id=?"
                + clause
                + " ORDER BY created_at DESC,post_id DESC LIMIT ?",
                args,
            ).fetchall()
            return [_post(row[0]) for row in rows]

    def pending_posts(self, limit: int = 100) -> list[Post]:
        _limit(limit)
        with self._database.connect() as db:
            rows = db.execute(
                "SELECT payload FROM posts WHERE pending=1 ORDER BY last_attempt,created_at,post_id LIMIT ?",
                (limit,),
            ).fetchall()
            return [_post(row[0]) for row in rows]

    def mark_dispatched(self, post_id: str) -> None:
        with self._database.connect() as db:
            db.execute("UPDATE posts SET pending=0 WHERE post_id=?", (post_id,))

    def is_pending(self, post_id: str) -> bool:
        with self._database.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM posts WHERE post_id=? AND pending=1", (post_id,)
                ).fetchone()
                is not None
            )

    def defer(self, post_id: str) -> None:
        with self._database.connect() as db:
            db.execute(
                "UPDATE posts SET last_attempt=(SELECT coalesce(max(last_attempt),0)+1 FROM posts WHERE pending=1) WHERE post_id=? AND pending=1",
                (post_id,),
            )


class SQLiteUserStore:
    def __init__(self, path: str) -> None:
        self._database = SQLiteDatabase(path)

    def put(self, profile: UserProfile) -> None:
        payload = json.dumps(dataclasses.asdict(profile), ensure_ascii=False)
        with self._database.connect() as db:
            db.execute(
                "INSERT INTO user_profiles VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
                (profile.user_id, payload),
            )

    def get(self, user_id: str) -> Optional[UserProfile]:
        with self._database.connect() as db:
            row = db.execute(
                "SELECT payload FROM user_profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            return UserProfile(**json.loads(row[0])) if row else None
