"""Indexed persistent social graph for a local deployment."""

from news_feed_system.concurrency import run_sync

from news_feed_system.social_graph import SocialGraph
from news_feed_system.storage import SQLiteDatabase


class SQLiteSocialGraph(SocialGraph):
    def __init__(self, path: str, max_following: int = 5000) -> None:
        if (
            isinstance(max_following, bool)
            or not isinstance(max_following, int)
            or max_following <= 0
        ):
            raise ValueError("max_following must be a positive integer")
        self._database = SQLiteDatabase(path)
        self._max_following = max_following

    def _add(self, follower_id: str, followee_id: str) -> None:
        with self._database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            ).fetchone()
            if existing:
                return
            count = db.execute(
                "SELECT count(*) FROM follows WHERE follower_id=?", (follower_id,)
            ).fetchone()[0]
            if count >= self._max_following:
                raise ValueError("Following limit reached")
            db.execute("INSERT INTO follows VALUES(?,?)", (follower_id, followee_id))

    async def add_follow(self, follower_id: str, followee_id: str) -> None:
        await run_sync(self._add, follower_id, followee_id)

    def _query(self, sql: str, args: tuple) -> list[tuple]:
        with self._database.connect() as db:
            return db.execute(sql, args).fetchall()

    async def remove_follow(self, follower_id: str, followee_id: str) -> None:
        await run_sync(
            self._query,
            "DELETE FROM follows WHERE follower_id=? AND followee_id=?",
            (follower_id, followee_id),
        )

    async def get_followers(self, user_id: str) -> list[str]:
        rows = await run_sync(
            self._query,
            "SELECT follower_id FROM follows WHERE followee_id=? ORDER BY follower_id",
            (user_id,),
        )
        return [row[0] for row in rows]

    async def get_following(self, user_id: str) -> list[str]:
        rows = await run_sync(
            self._query,
            "SELECT followee_id FROM follows WHERE follower_id=? ORDER BY followee_id",
            (user_id,),
        )
        return [row[0] for row in rows]

    async def get_follower_count(self, user_id: str) -> int:
        rows = await run_sync(
            self._query, "SELECT count(*) FROM follows WHERE followee_id=?", (user_id,)
        )
        return rows[0][0]

    async def is_following(self, follower_id: str, followee_id: str) -> bool:
        rows = await run_sync(
            self._query,
            "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
            (follower_id, followee_id),
        )
        return bool(rows)
