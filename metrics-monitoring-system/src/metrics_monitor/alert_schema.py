"""Idempotent schema, migrations and scheduling/GC indexes for alert state."""

from contextlib import closing


def initialize(store):
    with closing(store.connect()) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS rules(id TEXT PRIMARY KEY,body TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,rule_id TEXT,series TEXT,state TEXT,since REAL,transition INTEGER,updated REAL,no_data INTEGER,labels TEXT,value REAL);
        CREATE TABLE IF NOT EXISTS silences(id TEXT PRIMARY KEY,match TEXT,start REAL,end REAL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,alert_id TEXT,transition INTEGER,group_key TEXT,destination TEXT,channel TEXT,payload TEXT,due REAL,batch TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS deliveries(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,group_key TEXT,destination TEXT,channel TEXT,payload TEXT,due REAL,attempts INTEGER,lease TEXT,lease_until REAL,status TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS evaluations(rule_id TEXT PRIMARY KEY,version INTEGER,updated REAL);
        CREATE INDEX IF NOT EXISTS alerts_rule ON alerts(rule_id,series);
        CREATE INDEX IF NOT EXISTS silences_active ON silences(end,start);
        CREATE INDEX IF NOT EXISTS events_pending ON events(status,batch,due,id);
        CREATE INDEX IF NOT EXISTS events_batch ON events(batch);
        CREATE INDEX IF NOT EXISTS deliveries_ready ON deliveries(status,due,lease_until,seq);
        CREATE INDEX IF NOT EXISTS deliveries_fifo ON deliveries(group_key,status,seq);
        """)
        # Columns added for metadata GC; the check/ALTER pair is serialized so
        # concurrently starting processes cannot both add the same column.
        db.execute("BEGIN IMMEDIATE")
        if not _has_column(db, "events", "created"):
            db.execute("ALTER TABLE events ADD COLUMN created REAL")
            # Existing events carry their creation (evaluation) time in payload.
            db.execute("UPDATE events SET created=json_extract(payload,'$.timestamp')")
        if not _has_column(db, "evaluations", "gc_floor"):
            db.execute(
                "ALTER TABLE evaluations ADD COLUMN gc_floor INTEGER NOT NULL DEFAULT 0"
            )
        db.execute("CREATE INDEX IF NOT EXISTS events_gc ON events(status,created)")
        db.execute("CREATE INDEX IF NOT EXISTS events_alert ON events(alert_id,status)")
        db.commit()


def _has_column(db, table, column):
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))
