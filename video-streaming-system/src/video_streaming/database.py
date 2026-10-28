"""PostgreSQL metadata connection with failover discovery and fenced
transactions.
"""

import sqlite3
import collections
import threading
import time

try:
    import psycopg
except ImportError:
    psycopg = None  # Optional PostgreSQL backend.


class MetadataUnavailable(sqlite3.OperationalError):
    """Operation failed; its commit outcome may be unknown.

    Do not replay blindly.
    """


class Row(tuple):
    """A tuple-backed result row supporting column names and integer
    indices.
    """

    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row.names = names
        return row

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return super().__getitem__(
            self.names.index(key) if isinstance(key, str) else key
        )


class Result:
    """Buffered cursor results exposing the SQLite-style service interface."""

    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        names = (
            [column.name for column in cursor.description] if cursor.description else []
        )
        self.rows = (
            collections.deque(Row(row, names) for row in cursor.fetchall())
            if names
            else collections.deque()
        )

    def fetchone(self):
        return self.rows.popleft() if self.rows else None

    def fetchall(self):
        rows = list(self.rows)
        self.rows.clear()
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        if not self.rows:
            raise StopIteration
        return self.rows.popleft()


class Postgres:
    # One cluster-wide lock keeps all read-modify-write state transitions
    # atomic.
    """A PostgreSQL adapter with serialized transactions and bounded
    reconnection.
    """

    LOCK = 571501

    def __init__(self, dsn):
        if psycopg is None:
            raise ImportError("Install the distributed extra")

        self.driver, self.dsn = psycopg, dsn
        self.connection, self.active = None, False
        self._lock = threading.RLock()
        self._closed = False

        class BoundedConnection(psycopg.Connection):
            """Limit socket waits independently of the server statement
            timeout.
            """

            def wait(self, generator, interval=0.1, **kwargs):
                """Drive libpq with a fixed poll interval and a five-second
                bound.
                """
                del (
                    interval,
                    kwargs,
                )  # A fixed poll interval enforces the deadline.
                deadline = time.monotonic() + 5

                def bounded():
                    try:
                        state = next(generator)
                        while True:
                            ready = yield state
                            if time.monotonic() >= deadline:
                                raise psycopg.OperationalError(
                                    "metadata socket response deadline exceeded"
                                )
                            state = generator.send(ready)
                    except StopIteration as result:
                        return result.value
                    finally:
                        generator.close()

                return super().wait(bounded(), interval=0.1)

        self.connection_class = BoundedConnection

    def _discard(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def _ensure(self):
        if self._closed:
            raise RuntimeError("metadata adapter is closed")
        if self.connection is not None and not self.connection.closed:
            readonly = self.connection.execute("SHOW transaction_read_only").fetchone()[
                0
            ]
            if readonly == "on":
                self._discard()
        if self.connection is None or self.connection.closed:
            self.connection = self.connection_class.connect(
                self.dsn,
                autocommit=True,
                target_session_attrs="read-write",
                connect_timeout=3,
                keepalives=1,
                keepalives_idle=2,
                keepalives_interval=1,
                keepalives_count=3,
            )
            self.connection.execute("SET statement_timeout='4s'")
            self.connection.execute("SET lock_timeout='3s'")

    def __enter__(self):
        # Ownership spans the entire transaction, not just individual execute
        # calls; another thread cannot accidentally join an active transaction.
        self._lock.acquire()
        try:
            if self.active:
                raise RuntimeError("nested metadata transaction")
            try:
                self._ensure()
                self.connection.execute("BEGIN")
                self.connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (self.LOCK,)
                )
                self.active = True
                return self
            except self.driver.Error:
                self._discard()
                raise MetadataUnavailable(
                    "metadata unavailable; transaction not replayed"
                ) from None
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, kind, value, traceback):
        try:
            if self.connection is not None:
                self.connection.execute("ROLLBACK" if kind else "COMMIT")
        except self.driver.Error:
            self._discard()
            if kind is None:
                raise MetadataUnavailable(
                    "commit outcome unknown; reconcile persisted state"
                ) from None
        finally:
            self.active = False
            self._lock.release()

    def execute(self, sql, params=()):
        """Execute parameterized service SQL and return buffered result rows.

        Args:
            sql: Service SQL using question-mark parameter placeholders.
            params: Values bound separately from the SQL text.

        Returns:
            A Result for ordinary SQL, or None for the BEGIN IMMEDIATE
            compatibility
            marker within an existing transaction.

        Raises:
            MetadataUnavailable: The database operation fails; do not replay a
                write
                whose commit outcome may be unknown.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("metadata adapter is closed")
            return self._execute(sql, params)

    def _execute(self, sql, params):
        if sql == "BEGIN IMMEDIATE":
            if not self.active:
                raise RuntimeError("transaction context required")
            return None
        # SQL templates are internal, never user supplied. Parameters remain
        # bound.
        ignore = sql.startswith("INSERT OR IGNORE ")
        sql = sql.replace("INSERT OR IGNORE ", "INSERT ").replace("?", "%s")
        sql = sql.replace("ORDER BY priority DESC,rowid", "ORDER BY priority DESC,id")
        if ignore:
            sql += " ON CONFLICT DO NOTHING"
        try:
            if not self.active:
                self._ensure()
            return Result(self.connection.execute(sql, params))
        except self.driver.Error:
            self._discard()
            raise MetadataUnavailable(
                "metadata operation failed; transaction not replayed"
            ) from None

    def executescript(self, script):
        """Initialize semicolon-separated schema statements in one
        transaction.
        """
        with self:
            for sql in script.split(";"):
                if sql.strip():
                    # PostgreSQL REAL is float32: expiry timestamps require
                    # float64.
                    self.execute(sql.replace(" REAL", " DOUBLE PRECISION"))

    def close(self):
        """Drain another thread's transaction and permanently close this adapter."""
        with self._lock:
            if self.active:
                raise RuntimeError("cannot close an active metadata transaction")
            self._closed = True
            self._discard()
