"""Durable series, retention tiers and incremental rollup work."""

from contextlib import closing


def initialize(store, max_series, per_metric):
    for value in (max_series, per_metric):
        if type(value) is not int or not 1 <= value <= 100000:
            raise ValueError("cardinality budget must be an integer in 1..100000")
    with closing(store.connect()) as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER);
        CREATE TABLE IF NOT EXISTS kinds(metric TEXT PRIMARY KEY,kind TEXT);
        CREATE TABLE IF NOT EXISTS series(id TEXT PRIMARY KEY,metric TEXT,labels TEXT,kind TEXT);
        CREATE INDEX IF NOT EXISTS series_metric ON series(metric);
        CREATE TABLE IF NOT EXISTS tags(series TEXT,key TEXT,value TEXT,PRIMARY KEY(series,key));
        CREATE INDEX IF NOT EXISTS tags_lookup ON tags(key,value,series);
        CREATE TABLE IF NOT EXISTS raw(series TEXT,timestamp REAL,value REAL,PRIMARY KEY(series,timestamp));
        CREATE INDEX IF NOT EXISTS raw_time ON raw(timestamp);
        CREATE TABLE IF NOT EXISTS rollups(series TEXT,grain INTEGER,bucket INTEGER,stats TEXT,PRIMARY KEY(series,grain,bucket));
        CREATE INDEX IF NOT EXISTS rollups_age ON rollups(grain,bucket);
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,hash TEXT,result TEXT,received REAL);
        CREATE TABLE IF NOT EXISTS rejections(id TEXT PRIMARY KEY,hash TEXT,reason TEXT,received REAL);
        CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,updated REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS dirty_minutes(series TEXT,bucket INTEGER,PRIMARY KEY(series,bucket));
        CREATE INDEX IF NOT EXISTS dirty_minutes_age ON dirty_minutes(bucket);
        CREATE TABLE IF NOT EXISTS dirty_hours(series TEXT,bucket INTEGER,PRIMARY KEY(series,bucket));
        CREATE INDEX IF NOT EXISTS dirty_hours_age ON dirty_hours(bucket);
        """)
        for key, value in {
            "revision": 0,
            "compact_minute": -1,
            "max_series": max_series,
            "per_metric": per_metric,
        }.items():
            db.execute("INSERT OR IGNORE INTO meta VALUES(?,?)", (key, value))
        if not db.execute(
            "SELECT 1 FROM meta WHERE key='incremental_rollups'"
        ).fetchone():
            # Existing archives/summary hashes are left alone. Rebuild available
            # lower tiers once, then track only buckets touched by later writes.
            db.execute(
                "INSERT OR IGNORE INTO dirty_minutes SELECT series,CAST(timestamp/60 AS INTEGER)*60 FROM raw"
            )
            db.execute(
                "INSERT OR IGNORE INTO dirty_hours SELECT series,CAST(bucket/3600 AS INTEGER)*3600 FROM rollups WHERE grain=60"
            )
            db.execute("INSERT INTO meta VALUES('incremental_rollups',1)")
            db.execute("UPDATE meta SET value=-1 WHERE key='compact_minute'")
        db.commit()
