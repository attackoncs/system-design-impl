"""Shared portable metadata schema; runtime transitions live in their repositories."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (token TEXT PRIMARY KEY, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY, owner TEXT, title TEXT, size INTEGER, part_size INTEGER,
    state TEXT, source TEXT, assets TEXT, key_hex TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS parts (
    video TEXT, idx INTEGER, key TEXT, size INTEGER, PRIMARY KEY(video, idx)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, video TEXT, name TEXT, kind TEXT, profile INTEGER,
    deps TEXT, state TEXT, attempts INTEGER, due REAL, lease TEXT, expires REAL,
    output TEXT, error TEXT, priority INTEGER, UNIQUE(video, name)
);
CREATE TABLE IF NOT EXISTS completions (
    video TEXT PRIMARY KEY, assets TEXT, handled INTEGER
);
CREATE TABLE IF NOT EXISTS job_limits (
    job TEXT PRIMARY KEY, attempts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state, priority, due);
CREATE INDEX IF NOT EXISTS expired_jobs ON jobs(state, expires);
CREATE INDEX IF NOT EXISTS video_jobs ON jobs(video, state);
CREATE INDEX IF NOT EXISTS pending_completions ON completions(handled);
"""
