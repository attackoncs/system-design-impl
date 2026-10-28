"""Retained profile history, projections, sessions and leased outbox schema."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,version INTEGER,profile TEXT);
        CREATE TABLE IF NOT EXISTS requests(user TEXT,id TEXT,hash TEXT,result TEXT,PRIMARY KEY(user,id));
        CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,user TEXT,version INTEGER,profile TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,payload TEXT,state TEXT,lease REAL,token TEXT,attempts INTEGER);
        CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS replication_source(id INTEGER PRIMARY KEY CHECK(id=1),source_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS topology(id INTEGER PRIMARY KEY CHECK(id=1),count INTEGER,partition INTEGER);
        CREATE TRIGGER IF NOT EXISTS changes_no_update BEFORE UPDATE ON changes BEGIN SELECT RAISE(ABORT,'immutable profile history'); END;
        CREATE TRIGGER IF NOT EXISTS changes_no_delete BEFORE DELETE ON changes BEGIN SELECT RAISE(ABORT,'immutable profile history'); END;
"""
