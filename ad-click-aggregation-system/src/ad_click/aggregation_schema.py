"""Idempotent aggregation schema and replay-position/state-GC migration."""

from .common import Error


def _add_column(db, table, column, ddl):
    names = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in names:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def initialize(store, partition):
    store.initialize("""
    CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), partition INTEGER,
        lineage TEXT, seq INTEGER, watermark REAL, late INTEGER, max_event REAL);
    CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY, hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS buckets(minute INTEGER, ad_id TEXT, user_id TEXT, ip TEXT, country TEXT,
        count INTEGER NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(minute,ad_id,user_id,ip,country));
    CREATE INDEX IF NOT EXISTS by_ad ON buckets(ad_id,minute);
    CREATE INDEX IF NOT EXISTS by_country ON buckets(country,minute);
    CREATE INDEX IF NOT EXISTS by_user ON buckets(user_id,minute);
    CREATE INDEX IF NOT EXISTS by_ip ON buckets(ip,minute);
    CREATE TABLE IF NOT EXISTS input_positions(seq INTEGER PRIMARY KEY,event_id TEXT NOT NULL,hash TEXT NOT NULL,accepted REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS worker_meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
    """)
    with store.transaction(True) as db:
        # Additive migrations: existing rows get NULL event time (kept forever,
        # never range-deleted) and a zero expired-event counter.
        _add_column(db, "seen", "click_timestamp", "INTEGER")
        _add_column(db, "checkpoint", "expired", "INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "CREATE INDEX IF NOT EXISTS seen_by_time ON seen(click_timestamp)"
        )
        db.execute(
            "INSERT OR IGNORE INTO checkpoint(id,partition,lineage,seq,watermark,late,max_event,expired)"
            " VALUES(1,?,'',0,0,0,0,0)",
            (partition,),
        )
        cp = db.execute("SELECT * FROM checkpoint").fetchone()
        if cp["partition"] != partition:
            raise Error(409, "worker database belongs to another partition")
        # Legacy databases have ID deduplication but no offset-to-ID receipts.
        # Require matching seen IDs there; every new position gets a full receipt.
        db.execute(
            "INSERT OR IGNORE INTO worker_meta VALUES('position_floor',?)",
            (cp["seq"] + 1,),
        )
        # Positions below replay_floor had their receipts garbage-collected:
        # replaying them fails explicitly instead of being trusted.
        db.execute("INSERT OR IGNORE INTO worker_meta VALUES('replay_floor',0)")
