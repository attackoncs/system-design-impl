"""Durable reference metadata schema (SQLite WAL)."""

import secrets
import sqlite3
import time
import unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,quota INTEGER,used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user TEXT);
            CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,owner TEXT,name TEXT);
            CREATE TABLE IF NOT EXISTS members(space TEXT,user TEXT,role TEXT,PRIMARY KEY(space,user));
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,space TEXT,path TEXT,head TEXT,live_path_key TEXT,UNIQUE(space,path));
            CREATE TABLE IF NOT EXISTS revisions(version INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,file TEXT,size INTEGER,checksum TEXT,blocks TEXT,deleted INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,user TEXT,space TEXT,path TEXT,base TEXT,
                size INTEGER,checksum TEXT,blocks TEXT,state TEXT,result TEXT,created REAL,request TEXT,UNIQUE(user,request));
            CREATE TABLE IF NOT EXISTS blocks(space TEXT,digest TEXT,size INTEGER,key TEXT,refs INTEGER,created REAL,repair_needed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(space,digest));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,user TEXT,kind TEXT,space TEXT,file TEXT,revision TEXT,path TEXT,created REAL);
            -- Retention bookkeeping per space: every event of the space with seq<=floor
            -- has been pruned; retained counts the rows still present.
            CREATE TABLE IF NOT EXISTS event_spaces(space TEXT PRIMARY KEY,floor INTEGER NOT NULL DEFAULT 0,retained INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,user TEXT,cursor INTEGER,delivered INTEGER);
            CREATE INDEX IF NOT EXISTS event_user ON events(user,seq);
CREATE INDEX IF NOT EXISTS revision_file ON revisions(file,version);
CREATE INDEX IF NOT EXISTS open_uploads ON uploads(state,space);
CREATE INDEX IF NOT EXISTS orphan_blocks ON blocks(refs,created);
CREATE INDEX IF NOT EXISTS space_owner ON spaces(owner);
CREATE INDEX IF NOT EXISTS event_space ON events(space,seq);
CREATE INDEX IF NOT EXISTS member_user ON members(user);
"""


def _enable_wal(db):
    deadline = time.monotonic() + 10
    while True:
        try:
            db.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _upgrade_live_paths(db):
    missing_column = "live_path_key" not in {
        row[1] for row in db.execute("PRAGMA table_info(files)")
    }
    if missing_column:
        db.execute("ALTER TABLE files ADD COLUMN live_path_key TEXT")
    version = db.execute(
        "SELECT value FROM settings WHERE key='path_unicode_version'"
    ).fetchone()
    if missing_column or version is None or version[0] != unicodedata.unidata_version:
        # SQLite's lower() handles ASCII. Preserve the Python casefold used by callers.
        db.create_function("fold_path", 1, str.casefold)
        try:
            db.execute(
                "UPDATE files SET live_path_key=fold_path(path) WHERE EXISTS "
                "(SELECT 1 FROM revisions WHERE id=files.head AND deleted=0)"
            )
        finally:
            db.create_function("fold_path", 1, None)
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES('path_unicode_version',?)",
            (unicodedata.unidata_version,),
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS live_path ON files(space,live_path_key) "
        "WHERE live_path_key IS NOT NULL"
    )


def _upgrade(db, clock):
    _upgrade_live_paths(db)
    if "repair_needed" not in {
        row[1] for row in db.execute("PRAGMA table_info(blocks)")
    }:
        db.execute(
            "ALTER TABLE blocks ADD COLUMN repair_needed INTEGER NOT NULL DEFAULT 0"
        )
    if "used" not in {row[1] for row in db.execute("PRAGMA table_info(users)")}:
        # Pre-counter database: add and backfill users.used once,
        # atomically, from the revisions it summarizes.
        db.execute("ALTER TABLE users ADD COLUMN used INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "UPDATE users SET used=(SELECT COALESCE(SUM(r.size),0) FROM revisions r JOIN files f ON r.file=f.id JOIN spaces s ON f.space=s.id WHERE s.owner=users.name)"
        )
    if "created" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
        # Pre-retention database: start every retained event's age
        # now and count rows per space once; nothing was pruned yet.
        db.execute("ALTER TABLE events ADD COLUMN created REAL")
        db.execute("UPDATE events SET created=?", (clock(),))
        db.execute(
            "INSERT OR REPLACE INTO event_spaces(space,floor,retained) "
            "SELECT space,0,COUNT(*) FROM events GROUP BY space"
        )


def initialize(db, block_size, clock):
    """Open the schema and atomically upgrade old metadata before serving requests."""
    db.row_factory = sqlite3.Row
    _enable_wal(db)
    db.executescript(SCHEMA)
    with db:
        db.execute("BEGIN IMMEDIATE")
        _upgrade(db, clock)
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES('master',?)",
            (secrets.token_hex(32),),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings VALUES('block_size',?)", (str(block_size),)
        )
        settings = dict(
            db.execute(
                "SELECT key,value FROM settings WHERE key IN ('block_size','master')"
            )
        )
        if int(settings["block_size"]) != block_size:
            raise ValueError("block size differs from persisted configuration")
        return bytes.fromhex(settings["master"])
