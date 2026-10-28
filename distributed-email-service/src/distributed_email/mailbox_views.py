"""User-scoped mailbox projections and resumable, bounded long polling.

The durable change log remains the source of truth. Process-local caching only
reuses metadata at the exact database watermark; it never caches message bodies.
"""

from collections import OrderedDict
from copy import deepcopy
import json
import math
import threading
import time

from .common import Error, dumps, integer

FOLDERS = ("inbox", "sent", "spam", "quarantine")


class MailboxViews:
    def __init__(self, store, *, cache_entries=128, cache_bytes=4 * 1024 * 1024):
        integer(cache_entries, "cache entries", 0, 4096)
        integer(cache_bytes, "cache bytes", 0, 64 * 1024 * 1024)
        self.store = store
        self.capacity, self.byte_limit = cache_entries, cache_bytes
        self._cache, self._bytes = OrderedDict(), 0
        self._lock = threading.Lock()
        self._hits = self._misses = 0
        # Avoid scanning other users' history on every reconnect or poll.
        store.initialize("""
            CREATE INDEX IF NOT EXISTS changes_by_user ON changes(
                json_extract(payload,'$.record.user'),seq);
            CREATE INDEX IF NOT EXISTS folder_counts ON mails(
                user,deleted,json_extract(descriptor,'$.folder'),read);
        """)

    @staticmethod
    def _watermark(db):
        row = db.execute("SELECT lineage,position FROM metadata").fetchone()
        if not row["lineage"]:
            raise Error(503, "replica not initialized")
        return {"lineage": row["lineage"], "position": row["position"]}

    def watermark(self):
        with self.store.transaction() as db:
            return self._watermark(db)

    def ensure(self, position, lineage):
        integer(position, "min_position")
        if not isinstance(lineage, str) or not lineage:
            raise ValueError("read barrier requires lineage")
        current = self.watermark()
        if current["lineage"] != lineage:
            raise Error(409, "mailbox lineage changed; refresh mailbox")
        if current["position"] < position:
            raise Error(503, "mailbox has not reached required position")

    def _cached(self, key):
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            self._hits += 1
            value, _ = self._cache[key]
            self._cache.move_to_end(key)
            return deepcopy(value)

    def _remember(self, key, value):
        size = len(dumps(value).encode())
        if not self.capacity or size > self.byte_limit:
            return value
        with self._lock:
            old = self._cache.pop(key, None)
            if old:
                self._bytes -= old[1]
            self._cache[key] = (deepcopy(value), size)
            self._bytes += size
            while len(self._cache) > self.capacity or self._bytes > self.byte_limit:
                _, (_, removed) = self._cache.popitem(last=False)
                self._bytes -= removed
        return value

    def stats(self):
        with self._lock:
            return {
                "entries": len(self._cache),
                "bytes": self._bytes,
                "hits": self._hits,
                "misses": self._misses,
            }

    def folders(self, user):
        self.store.owner(user)
        with self.store.transaction() as db:
            watermark = self._watermark(db)
            key = ("folders", user, watermark["lineage"], watermark["position"])
            cached = self._cached(key)
            if cached is not None:
                return cached
            counts = {
                row["folder"]: (row["total"], row["unread"])
                for row in db.execute(
                    """
                    SELECT json_extract(descriptor,'$.folder') AS folder,
                           count(*) AS total,sum(read=0) AS unread
                    FROM mails WHERE user=? AND deleted=0 GROUP BY folder
                """,
                    (user,),
                )
            }
            value = {
                **watermark,
                "folders": [
                    {
                        "id": folder,
                        "name": folder,
                        "total": counts.get(folder, (0, 0))[0],
                        "unread": counts.get(folder, (0, 0))[1],
                    }
                    for folder in FOLDERS
                ],
            }
        return self._remember(key, value)

    def listing(self, user, **params):
        self.store.owner(user)
        watermark = self.watermark()
        filters = dumps(params)
        key = ("listing", user, watermark["lineage"], watermark["position"], filters)
        cached = self._cached(key)
        if cached is not None:
            return cached
        value = self.store.listing(user, **params)
        # A writer may have committed between watermark() and listing().
        actual_key = ("listing", user, value["lineage"], value["position"], filters)
        return self._remember(actual_key, value)

    def changes(self, user, after=0, lineage=None, wait=0, *, cancel=None):
        self.store.owner(user)
        integer(after, "after")
        if (
            type(wait) not in (int, float)
            or not math.isfinite(wait)
            or not 0 <= wait <= 2
        ):
            raise ValueError("wait must be between 0 and 2 seconds")
        if lineage is not None and (not isinstance(lineage, str) or not lineage):
            raise ValueError("invalid lineage")
        if after and lineage is None:
            raise ValueError("resuming requires lineage")
        end = time.monotonic() + wait
        sleeper = cancel or threading.Event()
        while True:
            with self.store.transaction() as db:
                current = self._watermark(db)
                if lineage is not None and lineage != current["lineage"]:
                    raise Error(409, "mailbox lineage changed; refresh mailbox")
                if after > current["position"]:
                    raise Error(409, "cursor is ahead of mailbox")
                first = db.execute("SELECT min(seq) FROM changes").fetchone()[0]
                if current["position"] > after and (first is None or first > after + 1):
                    raise Error(
                        409, "change history unavailable; refresh mailbox and cursor"
                    )
                rows = db.execute(
                    """
                    SELECT seq,payload FROM changes
                    WHERE json_extract(payload,'$.record.user')=? AND seq>?
                    ORDER BY seq LIMIT 101
                """,
                    (user, after),
                ).fetchall()
                events = []
                for row in rows[:100]:
                    mail = json.loads(row["payload"])["record"]
                    descriptor = json.loads(mail["descriptor"])
                    events.append(
                        {
                            "position": row["seq"],
                            "id": mail["id"],
                            "version": mail["version"],
                            "read": bool(mail["read"]),
                            "deleted": bool(mail["deleted"]),
                            "folder": descriptor["folder"],
                        }
                    )
                result = {
                    **current,
                    "events": events,
                    "more": len(rows) > 100,
                    "head": current["position"],
                }
                if result["more"]:
                    result["position"] = events[-1]["position"]
            remaining = end - time.monotonic()
            if events or remaining <= 0 or sleeper.is_set():
                return result
            sleeper.wait(min(0.05, remaining))
