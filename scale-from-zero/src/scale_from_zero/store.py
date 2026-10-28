"""Composition root for profile storage and its operational counters."""

import secrets
import threading
import time
from .persistence import ClockedDatabase
from .schema import SCHEMA
from .profiles import ProfileMixin
from .sessions import SessionMixin
from .jobs import JobMixin
from .replication import ReplicationMixin
from .routing import partition as partition
from .consumer import Consumer as Consumer


class Store(ProfileMixin, SessionMixin, JobMixin, ReplicationMixin, ClockedDatabase):
    def __init__(self, path, clock=time.time):
        super().__init__(path, clock)
        self.initialize(SCHEMA)
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO identity VALUES(1,?)", (secrets.token_hex(16),)
            )
        self._reads = 0
        self._metrics_lock = threading.Lock()

    @property
    def reads(self):
        with self._metrics_lock:
            return self._reads

    def status(self):
        with self.read_transaction() as db:
            return {
                "users": db.execute("SELECT count(*) FROM users").fetchone()[0],
                "seq": db.execute(
                    "SELECT coalesce(max(seq),0) FROM changes"
                ).fetchone()[0],
                "profile_reads": self.reads,
                "pending_jobs": db.execute(
                    "SELECT count(*) FROM jobs WHERE state!='done'"
                ).fetchone()[0],
            }
