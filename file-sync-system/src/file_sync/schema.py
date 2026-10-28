"""Durable reference metadata schema (SQLite WAL)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY,quota INTEGER);
            CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user TEXT);
            CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,owner TEXT,name TEXT);
            CREATE TABLE IF NOT EXISTS members(space TEXT,user TEXT,role TEXT,PRIMARY KEY(space,user));
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,space TEXT,path TEXT,head TEXT,UNIQUE(space,path));
            CREATE TABLE IF NOT EXISTS revisions(version INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,file TEXT,size INTEGER,checksum TEXT,blocks TEXT,deleted INTEGER,created REAL);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,user TEXT,space TEXT,path TEXT,base TEXT,
                size INTEGER,checksum TEXT,blocks TEXT,state TEXT,result TEXT,created REAL,request TEXT,UNIQUE(user,request));
            CREATE TABLE IF NOT EXISTS blocks(space TEXT,digest TEXT,size INTEGER,key TEXT,refs INTEGER,created REAL,PRIMARY KEY(space,digest));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,user TEXT,kind TEXT,space TEXT,file TEXT,revision TEXT,path TEXT);
            CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,user TEXT,cursor INTEGER,delivered INTEGER);
            CREATE INDEX IF NOT EXISTS event_user ON events(user,seq);
CREATE INDEX IF NOT EXISTS revision_file ON revisions(file,version);
CREATE INDEX IF NOT EXISTS open_uploads ON uploads(state,space);
CREATE INDEX IF NOT EXISTS orphan_blocks ON blocks(refs,created);
"""
