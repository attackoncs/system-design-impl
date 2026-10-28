import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import time

from .contracts import freeze

from .common import Database, Error, Fence, checksum, encode, entry, request, verify


class Replica(Fence):
    def __init__(self, path, epoch=1, leader="coordinator"):
        super().__init__(path)
        self.init_fence(epoch, leader)
        self.initialize("""
        CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,body TEXT);
        CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal replica'); END;
        CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal replica'); END;
        """)
        with self.connection() as db:
            verify(self.rows(db))

    def rows(self, db):
        return [
            json.loads(row[0])
            for row in db.execute("SELECT body FROM entries ORDER BY seq")
        ]

    def append(self, row, epoch, leader):
        row = freeze(row)
        with self.transaction() as db:
            self.check_fence(db, epoch, leader)
            seq = row["seq"]
            if type(seq) is not int or seq < 1:
                raise ValueError("invalid sequence")
            previous = db.execute(
                "SELECT body FROM entries ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            tail = json.loads(previous[0]) if previous else {"seq": 0, "hash": "0" * 64}
            if seq <= tail["seq"]:
                old = db.execute(
                    "SELECT body FROM entries WHERE seq=?", (seq,)
                ).fetchone()
                if old is None or json.loads(old[0]) != row:
                    raise Error(409, "log prefix conflict; operator recovery required")
            else:
                if seq != tail["seq"] + 1 or row != entry(
                    seq, tail["hash"], row["event"]
                ):
                    raise Error(409, "log gap or checksum conflict")
                db.execute("INSERT INTO entries VALUES(?,?)", (seq, encode(row)))
        return {"accepted": seq, "hash": row["hash"]}

    def status(self, epoch=None, leader=None):
        with self.read_transaction() as db:
            if epoch is not None:
                self.check_fence(db, epoch, leader)
            previous = db.execute(
                "SELECT body FROM entries ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            tail = json.loads(previous[0]) if previous else {"seq": 0, "hash": "0" * 64}
            return {"count": tail["seq"], "head": tail["hash"]}


class Journal(Database):
    """Fixed single writer: local durable intent plus 2-of-3 network replication.

    There is no election or alternate writer using a different authority disk.
    Retaining local pending intents is required for safe ambiguous-append recovery.
    """

    def __init__(self, path, config):
        super().__init__(path)
        self.config = freeze(config)
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.pool = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="wallet-replica"
        )
        if len(config["replicas"]) != 3 or len(set(config["replicas"])) != 3:
            raise ValueError("three distinct replica endpoints required")
        self.initialize("""
        CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,body TEXT,committed INTEGER CHECK(committed IN(0,1)));
        CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE OF body,seq ON entries BEGIN SELECT RAISE(ABORT,'immutable journal intent'); END;
        CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries BEGIN SELECT RAISE(ABORT,'immutable journal intent'); END;
        """)
        with self.connection() as db:
            rows = [
                json.loads(row[0])
                for row in db.execute("SELECT body FROM entries ORDER BY seq")
            ]
            verify(rows)
            committed = [
                r[0] for r in db.execute("SELECT committed FROM entries ORDER BY seq")
            ]
            if committed != sorted(committed, reverse=True):
                raise ValueError("committed log must be a prefix")
        self.initialize(
            "CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1),hash TEXT NOT NULL);"
        )
        fingerprint = checksum(
            {"accounts": self.config["accounts"], "shards": len(self.config["shards"])}
        )
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO identity VALUES(1,?)", (fingerprint,))
            if db.execute("SELECT hash FROM identity").fetchone()[0] != fingerprint:
                raise ValueError(
                    "account ownership/opening or shard topology changed; migration required"
                )

    @property
    def credentials(self):
        return {"epoch": self.config["epoch"], "leader": self.config["leader"]}

    def records(self, committed=True, after=0):
        with self.connection() as db:
            sql = (
                "SELECT body FROM entries"
                + (" WHERE committed=1" if committed else "")
                + (" AND" if committed else " WHERE")
                + " seq>? ORDER BY seq"
            )
            return [json.loads(row[0]) for row in db.execute(sql, (after,))]

    @contextmanager
    def guard(self, deadline, cancel):
        while True:
            self.live(deadline, cancel)
            if self.lock.acquire(
                timeout=max(0, min(0.05, deadline - time.monotonic()))
            ):
                break
        try:
            self.live(deadline, cancel)
            yield
        finally:
            self.lock.release()

    def live(self, deadline, cancel):
        if (
            self.closed.is_set()
            or (cancel is not None and cancel.is_set())
            or time.monotonic() >= deadline
        ):
            raise Error(503, "journal operation cancelled or expired")

    def _replicate(self, url, rows, deadline, cancel):
        try:

            def rpc(path, body):
                self.live(deadline, cancel)
                return request(
                    url + path,
                    self.config["internal_token"],
                    body,
                    timeout=0.5,
                    deadline=deadline,
                    cancel=cancel,
                )

            remote = rpc("/internal/status", self.credentials)
            if (
                not isinstance(remote, dict)
                or set(remote) != {"count", "head"}
                or type(remote["count"]) is not int
                or not 0 <= remote["count"] <= len(rows)
            ):
                return False
            count = remote["count"]
            if remote["head"] != (rows[count - 1]["hash"] if count else "0" * 64):
                return False
            for row in rows[count:]:
                ack = rpc("/internal/append", {**self.credentials, "entry": row})
                if (
                    not isinstance(ack, dict)
                    or type(ack.get("accepted")) is not int
                    or ack != {"accepted": row["seq"], "hash": row["hash"]}
                ):
                    return False
            return True
        except (Error, OSError, TimeoutError, ValueError, TypeError):
            return False

    def _sync(self, deadline, cancel):
        rows = self.records(False)
        verify(rows)
        # Wait for every accepted task, even after a quorum or an error. The
        # three owned workers never outlive this snapshot's writer ownership.
        futures, failure, acknowledgements = [], None, 0
        try:
            for url in self.config["replicas"]:
                futures.append(
                    self.pool.submit(self._replicate, url, rows, deadline, cancel)
                )
        except BaseException as exc:
            failure = exc
        for future in futures:
            try:
                acknowledgements += bool(future.result())
            except BaseException as exc:
                # An unexpected adapter failure must not release writer
                # ownership while other accepted replica tasks are still live.
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
        self.live(deadline, cancel)
        if acknowledgements < 2:
            raise Error(
                503, "journal quorum unavailable; retain original transaction identity"
            )
        with self.transaction() as db:
            db.execute(
                "UPDATE entries SET committed=1 WHERE seq<=? AND committed=0",
                (len(rows),),
            )
        return {"acks": acknowledgements, "events": len(rows)}

    def has_pending(self):
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM entries WHERE committed=0 LIMIT 1").fetchone()
                is not None
            )

    def sync(self, *, deadline=None, cancel=None):
        deadline = time.monotonic() + 10 if deadline is None else deadline
        with self.guard(deadline, cancel):
            return self._sync(deadline, cancel)

    def append(self, event, *, deadline=None, cancel=None):
        event = freeze(event)  # Freeze before waiting for the single writer.
        deadline = time.monotonic() + 10 if deadline is None else deadline
        with self.guard(deadline, cancel):
            self._sync(deadline, cancel)
            with self.transaction() as db:
                old = db.execute(
                    "SELECT body FROM entries ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                previous = json.loads(old[0]) if old else {"seq": 0, "hash": "0" * 64}
                row = entry(previous["seq"] + 1, previous["hash"], event)
                db.execute(
                    "INSERT INTO entries VALUES(?,?,0)", (row["seq"], encode(row))
                )
            self._sync(deadline, cancel)
            return row

    def close(self):
        self.closed.set()
        with self.lock:
            self.pool.shutdown(wait=True, cancel_futures=True)
