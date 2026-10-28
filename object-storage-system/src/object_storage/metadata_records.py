"""Validated, privately owned values for object metadata transactions."""

import math

from .common import CHUNK, checksum, identifier, integer, object_key, sha

MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 256

SCHEMA = """
CREATE TABLE IF NOT EXISTS topology(
    id INTEGER PRIMARY KEY CHECK(id=1),shard INTEGER,shards INTEGER);
CREATE TABLE IF NOT EXISTS versions(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,bucket TEXT,key TEXT,
    marker INTEGER,manifest TEXT,size INTEGER,sha TEXT,created REAL,removed REAL);
CREATE INDEX IF NOT EXISTS lookup ON versions(bucket,key,seq);
CREATE INDEX IF NOT EXISTS live_versions ON versions(bucket,removed,key,seq);
CREATE TABLE IF NOT EXISTS receipts(
    bucket TEXT,key TEXT,request TEXT,hash TEXT,version TEXT,
    PRIMARY KEY(bucket,key,request));
"""


def object_identity(bucket, key):
    """Bucket IDs are opaque catalog identifiers, never filesystem paths."""
    return identifier(bucket), object_key(key)


def manifest_copy(manifest):
    if not isinstance(manifest, list) or len(manifest) > MAX_CHUNKS:
        raise ValueError("invalid manifest")
    entries = tuple(manifest)
    if len(entries) > MAX_CHUNKS:
        raise ValueError("invalid manifest")
    frozen = []
    for chunk in entries:
        if not isinstance(chunk, dict):
            raise ValueError("invalid chunk manifest")
        # Extract once; the list and dictionaries used by the transaction have
        # no references back to caller-owned mutable containers.
        value = dict(chunk)
        if set(value) != {"hash", "size"}:
            raise ValueError("invalid chunk manifest")
        frozen.append(
            {
                "hash": checksum(value["hash"]),
                "size": integer(value["size"], "chunk size", 1, CHUNK),
            }
        )
    return frozen


def publication(bucket, key, request, manifest, size, checksum_value, marker):
    object_identity(bucket, key)
    identifier(request)
    checksum(checksum_value)
    integer(size, "size", 0, MAX_OBJECT_BYTES)
    if type(marker) is not bool:
        raise ValueError("marker must be boolean")
    frozen = manifest_copy(manifest)
    if sum(chunk["size"] for chunk in frozen) != size:
        raise ValueError("manifest size mismatch")
    if marker and (frozen or size):
        raise ValueError("delete marker cannot contain data")
    if size == 0 and checksum_value != sha(b""):
        raise ValueError("empty object checksum mismatch")
    return frozen


def timestamp(clock):
    value = clock()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("clock must return a finite non-negative timestamp")
    return value
