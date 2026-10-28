"""Idempotent SQLite schema; existing state is upgraded in place."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY,token TEXT UNIQUE,admin INTEGER,
    sharing INTEGER DEFAULT 0,history INTEGER DEFAULT 0,
    revision INTEGER DEFAULT 0,last_ts REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests(
    sender TEXT,recipient TEXT,PRIMARY KEY(sender,recipient)
);
CREATE TABLE IF NOT EXISTS friends(a TEXT,b TEXT,PRIMARY KEY(a,b));
CREATE INDEX IF NOT EXISTS friends_recipient ON friends(b,a);
CREATE TABLE IF NOT EXISTS current(user TEXT PRIMARY KEY,body TEXT,expires REAL);
CREATE INDEX IF NOT EXISTS current_expiry ON current(expires);
CREATE TABLE IF NOT EXISTS history(
    user TEXT,revision INTEGER,body TEXT,received REAL,PRIMARY KEY(user,revision)
);
CREATE INDEX IF NOT EXISTS history_received ON history(received);
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY,value TEXT);
"""
