"""Metadata schema and restart handling for unfinished output stages.

Ordinary pending appends remain durable and can be quorum-published later. A
transaction stage has no separately visible commit decision; restart abandons
all such stages while leaving speculative broker tails subject to alignment.
"""

import json


def initialize(db):
    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS topics(name TEXT PRIMARY KEY,partitions INTEGER,retention REAL,max_bytes INTEGER,segment_bytes INTEGER);
    CREATE TABLE IF NOT EXISTS parts(topic TEXT,partition INTEGER,leader TEXT,epoch INTEGER,hwm INTEGER,floor INTEGER,base_hash TEXT,PRIMARY KEY(topic,partition));
    CREATE TABLE IF NOT EXISTS records(topic TEXT,partition INTEGER,offset INTEGER,hash TEXT,timestamp REAL,size INTEGER,PRIMARY KEY(topic,partition,offset));
    CREATE TABLE IF NOT EXISTS producers(id TEXT PRIMARY KEY,epoch INTEGER);
    CREATE TABLE IF NOT EXISTS sequences(producer TEXT,epoch INTEGER,topic TEXT,partition INTEGER,last INTEGER,PRIMARY KEY(producer,epoch,topic,partition));
    CREATE TABLE IF NOT EXISTS receipts(producer TEXT,epoch INTEGER,topic TEXT,partition INTEGER,sequence INTEGER,hash TEXT,response TEXT,last_offset INTEGER,PRIMARY KEY(producer,epoch,topic,partition,sequence));
    CREATE TABLE IF NOT EXISTS pending(topic TEXT,partition INTEGER,body TEXT,PRIMARY KEY(topic,partition));
    CREATE TABLE IF NOT EXISTS groups(topic TEXT,name TEXT,generation INTEGER,PRIMARY KEY(topic,name));
    CREATE TABLE IF NOT EXISTS members(topic TEXT,group_name TEXT,id TEXT,session TEXT,expires REAL,PRIMARY KEY(topic,group_name,id));
    CREATE TABLE IF NOT EXISTS cursors(topic TEXT,group_name TEXT,partition INTEGER,next INTEGER,PRIMARY KEY(topic,group_name,partition));
    CREATE TABLE IF NOT EXISTS delivered(topic TEXT,group_name TEXT,partition INTEGER,generation INTEGER,next INTEGER,PRIMARY KEY(topic,group_name,partition));
    CREATE TABLE IF NOT EXISTS transactions(id TEXT PRIMARY KEY,hash TEXT,response TEXT);
    CREATE TABLE IF NOT EXISTS retention_pending(topic TEXT,partition INTEGER,node TEXT,base INTEGER,base_hash TEXT,PRIMARY KEY(topic,partition,node));
    CREATE INDEX IF NOT EXISTS records_time ON records(topic,partition,timestamp);
    """)
    # Incomplete transactional output stages never independently become visible.
    for row in db.execute("SELECT topic,partition,body FROM pending").fetchall():
        if json.loads(row["body"]).get("transaction"):
            db.execute(
                "DELETE FROM pending WHERE topic=? AND partition=?",
                (row["topic"], row["partition"]),
            )
    db.commit()
