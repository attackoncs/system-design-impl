"""PostgreSQL metadata connection with failover discovery and fenced transactions."""
import sqlite3
import time


class MetadataUnavailable(sqlite3.OperationalError):
    """Operation failed; its commit outcome may be unknown. Do not replay blindly."""


class Row(tuple):
    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row.names = names
        return row

    def keys(self):
        return self.names

    def __getitem__(self, key):
        return super().__getitem__(self.names.index(key) if isinstance(key, str) else key)


class Result:
    def __init__(self, cursor):
        self.rowcount = cursor.rowcount
        names = [column.name for column in cursor.description] if cursor.description else []
        self.rows = [Row(row, names) for row in cursor.fetchall()] if names else []

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.rows)


class Postgres:
    # One cluster-wide lock keeps all read-modify-write state transitions atomic.
    LOCK = 571501

    def __init__(self, dsn):
        import psycopg
        self.driver, self.dsn = psycopg, dsn
        self.connection, self.active = None, False
        class BoundedConnection(psycopg.Connection):
            def wait(connection, generator, interval=.1, **kwargs):
                deadline = time.monotonic() + 5
                def bounded():
                    try:
                        state = next(generator)
                        while True:
                            ready = yield state
                            if time.monotonic() >= deadline:
                                raise psycopg.OperationalError("metadata socket response deadline exceeded")
                            state = generator.send(ready)
                    except StopIteration as result:
                        return result.value
                    finally:
                        generator.close()
                return super().wait(bounded(), interval=.1)
        self.connection_class = BoundedConnection

    def _discard(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def _ensure(self):
        if self.connection is not None and not self.connection.closed:
            readonly = self.connection.execute("SHOW transaction_read_only").fetchone()[0]
            if readonly == "on":
                self._discard()
        if self.connection is None or self.connection.closed:
            self.connection = self.connection_class.connect(self.dsn, autocommit=True,
                target_session_attrs="read-write", connect_timeout=3,
                keepalives=1, keepalives_idle=2, keepalives_interval=1, keepalives_count=3)
            self.connection.execute("SET statement_timeout='4s'")
            self.connection.execute("SET lock_timeout='3s'")

    def __enter__(self):
        if self.active:
            raise RuntimeError("nested metadata transaction")
        try:
            self._ensure()
            self.connection.execute("BEGIN")
            self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.LOCK,))
            self.active = True
            return self
        except self.driver.Error:
            self._discard()
            raise MetadataUnavailable("metadata unavailable; transaction not replayed") from None

    def __exit__(self, kind, value, traceback):
        try:
            if self.connection is not None:
                self.connection.execute("ROLLBACK" if kind else "COMMIT")
        except self.driver.Error:
            self._discard()
            if kind is None:
                raise MetadataUnavailable("commit outcome unknown; reconcile persisted state") from None
        finally:
            self.active = False

    def execute(self, sql, params=()):
        if sql == "BEGIN IMMEDIATE":
            if not self.active:
                raise RuntimeError("transaction context required")
            return None
        # SQL templates are internal, never user supplied. Parameters remain bound.
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
            raise MetadataUnavailable("metadata operation failed; transaction not replayed") from None

    def executescript(self, script):
        with self:
            for sql in script.split(";"):
                if sql.strip():
                    # PostgreSQL REAL is float32: expiry timestamps require float64.
                    self.execute(sql.replace(" REAL", " DOUBLE PRECISION"))

    def close(self):
        self._discard()
