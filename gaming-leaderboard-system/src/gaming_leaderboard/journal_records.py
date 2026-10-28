"""Privately owned, validated records for the authoritative match journal."""

import math

from .common import current_month, identifier, integer, month_bounds

MAX_PLAYERS = 10000
MAX_SCORE = 1000000000
MAX_TIMESTAMP = 253370764799

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata(
    id INTEGER PRIMARY KEY CHECK(id=1),lineage TEXT,shards INTEGER,grace INTEGER,
    profile_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY,name TEXT,registered INTEGER);
CREATE TABLE IF NOT EXISTS seasons(month TEXT PRIMARY KEY,status TEXT,settlement TEXT);
CREATE TABLE IF NOT EXISTS scores(
    month TEXT,user TEXT,score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 1000000000),
    PRIMARY KEY(month,user));
CREATE INDEX IF NOT EXISTS score_order ON scores(month,score DESC,user);
CREATE TABLE IF NOT EXISTS matches(id TEXT PRIMARY KEY,hash TEXT,month TEXT,receipt TEXT);
CREATE TABLE IF NOT EXISTS heads(shard INTEGER PRIMARY KEY,seq INTEGER);
CREATE TABLE IF NOT EXISTS events(
    shard INTEGER,seq INTEGER,payload TEXT,hash TEXT,PRIMARY KEY(shard,seq));
"""


def timestamp(clock):
    value = clock()
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= MAX_TIMESTAMP
    ):
        raise ValueError("clock must return a finite non-negative UTC timestamp")
    # The season's following month must also fit the datetime representation.
    month_bounds(current_month(value))
    return value


def display_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("display name requires 1..128 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("display name cannot contain controls")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("display name requires valid Unicode") from None
    return value


def match_result(body):
    if not isinstance(body, dict):
        raise ValueError("match requires an object")
    record = dict(body)
    if set(record) != {"match_id", "winner", "players", "ended_at"}:
        raise ValueError(
            "match requires match_id,winner,players,ended_at; points are fixed at one"
        )
    match, winner = identifier(record["match_id"]), identifier(record["winner"])
    participants = record["players"]
    if not isinstance(participants, list):
        raise ValueError(
            "2..8 distinct registered participants including winner required"
        )
    players = tuple(participants)
    if not 2 <= len(players) <= 8:
        raise ValueError(
            "2..8 distinct registered participants including winner required"
        )
    players = tuple(identifier(player) for player in players)
    if len(set(players)) != len(players) or winner not in players:
        raise ValueError(
            "2..8 distinct registered participants including winner required"
        )
    ended_at = integer(record["ended_at"], "ended_at", 0, MAX_TIMESTAMP)
    month_bounds(current_month(ended_at))
    return {
        "match_id": match,
        "winner": winner,
        "players": sorted(players),
        "ended_at": ended_at,
    }


def event_payload(value):
    if not isinstance(value, dict) or set(value) != {"month", "user", "score"}:
        raise ValueError("invalid journal event")
    month_bounds(value["month"])
    return {
        "month": value["month"],
        "user": identifier(value["user"]),
        "score": integer(value["score"], "score", 0, MAX_SCORE),
    }
