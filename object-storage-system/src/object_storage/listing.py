"""Frozen cross-shard listings with literal prefixes and signed cursors."""

import base64
import hashlib
import hmac
import json
import math
import re
import uuid

from .common import Error, checksum, dumps, identifier, integer, object_key, request
from .metadata_records import timestamp

MAX_ROWS = 10_000
MAX_SNAPSHOTS = 128
MAX_OWNER_SNAPSHOTS = 32
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SNAPSHOT_TTL = 300


def _query(bucket, user, prefix, versions, limit, delimiter):
    identifier(bucket)
    identifier(user)
    integer(limit, "limit", 1, 1000)
    if type(versions) is not bool:
        raise ValueError("versions must be boolean")
    if (
        not isinstance(prefix, str)
        or len(prefix.encode("utf-8")) > 512
        or any(ord(char) < 32 for char in prefix)
    ):
        raise ValueError("invalid prefix")
    if delimiter not in ("", "/") or not isinstance(delimiter, str):
        raise ValueError("delimiter must be empty or slash")


def _decode(token, secret):
    try:
        if not isinstance(token, str) or not 1 <= len(token) <= 2048:
            raise ValueError("token length")
        encoded, signature = token.split(".")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", encoded) or not re.fullmatch(
            r"[a-f0-9]{64}", signature
        ):
            raise ValueError("token format")
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        value = json.loads(
            base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        )
        if not isinstance(value, dict) or set(value) != {"id", "index"}:
            raise ValueError("payload")
        snapshot = identifier(value["id"])
        index = integer(value["index"], "cursor", 1, MAX_ROWS)
        return snapshot, index
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise Error(400, "invalid continuation token") from None


def _encode(snapshot, index, secret):
    encoded = (
        base64.urlsafe_b64encode(dumps({"id": snapshot, "index": index}).encode())
        .decode()
        .rstrip("=")
    )
    return (
        encoded + "." + hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    )


def _row(value):
    if not isinstance(value, dict) or set(value) != {
        "key",
        "id",
        "seq",
        "marker",
        "size",
        "sha",
        "created",
    }:
        raise Error(502, "invalid metadata listing row")
    value = dict(value)
    try:
        object_key(value["key"])
        identifier(value["id"])
        integer(value["seq"], "sequence", 1)
        integer(value["marker"], "marker", 0, 1)
        integer(value["size"], "size", 0, 64 * 1024 * 1024)
        checksum(value["sha"])
        created = value["created"]
        if (
            type(created) not in (int, float)
            or not math.isfinite(created)
            or created < 0
        ):
            raise ValueError("created")
        if value["marker"] and value["size"]:
            raise ValueError("marker size")
    except (ValueError, TypeError, KeyError):
        raise Error(502, "invalid metadata listing values") from None
    return value


def _entries(rows, prefix, delimiter):
    entries, prefixes = [], set()
    for row in rows:
        if not row["key"].startswith(prefix):
            continue
        tail = row["key"][len(prefix) :]
        if delimiter and "/" in tail:
            prefixes.add(prefix + tail.split("/", 1)[0] + "/")
        else:
            entries.append({"kind": "object", "value": row})
    entries.extend({"kind": "prefix", "value": item} for item in prefixes)
    entries.sort(
        key=lambda item: (
            (item["value"]["key"], -item["value"]["seq"])
            if item["kind"] == "object"
            else (item["value"], 0)
        )
    )
    return entries


def _load_rows(raw, delimiter):
    try:
        if len(raw.encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot size")
        values = json.loads(raw)
        if not isinstance(values, list) or len(values) > MAX_ROWS:
            raise ValueError("snapshot shape")
        result = []
        for value in values:
            if isinstance(value, dict) and set(value) == {"kind", "value"}:
                if value["kind"] == "object":
                    result.append({"kind": "object", "value": _row(value["value"])})
                elif (
                    value["kind"] == "prefix"
                    and delimiter == "/"
                    and isinstance(value["value"], str)
                ):
                    object_key(value["value"])
                    result.append(value)
                else:
                    raise ValueError("snapshot entry")
            elif delimiter == "":
                # Pre-upgrade recursive snapshots stored bare metadata rows.
                result.append({"kind": "object", "value": _row(value)})
            else:
                raise ValueError("snapshot entry")
        return result
    except (ValueError, TypeError, KeyError, Error):
        raise Error(409, "invalid stored listing snapshot") from None


def list_objects(
    config,
    catalog,
    bucket,
    user,
    prefix="",
    versions=False,
    limit=100,
    token=None,
    delimiter="",
    *,
    request_fn=request,
):
    _query(bucket, user, prefix, versions, limit, delimiter)
    secret = config["continuation_secret"].encode()
    with catalog.transaction() as db:
        catalog._owned_bucket(db, bucket, user)
    if token is not None:
        snapshot, index = _decode(token, secret)
        with catalog.transaction() as db:
            row = db.execute(
                "SELECT * FROM snapshots WHERE id=?", (snapshot,)
            ).fetchone()
            if (
                not row
                or (
                    row["user"],
                    row["bucket"],
                    row["prefix"],
                    bool(row["versions"]),
                    row["page_size"],
                    row["delimiter"],
                )
                != (user, bucket, prefix, versions, limit, delimiter)
                or row["expires"] <= timestamp(catalog.clock)
            ):
                raise Error(409, "continuation expired or query/owner changed")
            entries = _load_rows(row["rows"], delimiter)
        if index >= len(entries) or index % limit:
            raise Error(409, "invalid continuation offset")
    else:
        rows, ids, keys = [], set(), set()
        for url in config["metadata_urls"]:
            result = request_fn(
                url + "/list",
                config["internal_token"],
                {"bucket": bucket, "versions": versions},
            )
            if (
                not isinstance(result, dict)
                or set(result) != {"rows"}
                or not isinstance(result["rows"], list)
            ):
                raise Error(502, "invalid metadata listing response")
            if len(rows) + len(result["rows"]) > MAX_ROWS:
                raise Error(422, "global listing budget exceeded")
            for value in result["rows"]:
                row = _row(value)
                if row["id"] in ids or (
                    not versions and (row["marker"] or row["key"] in keys)
                ):
                    raise Error(502, "conflicting metadata listing rows")
                ids.add(row["id"])
                keys.add(row["key"])
                rows.append(row)
        entries = _entries(rows, prefix, delimiter)
        snapshot, index = uuid.uuid4().hex, 0
        if len(entries) > limit:
            raw = dumps(entries)
            if len(raw.encode()) > MAX_SNAPSHOT_BYTES:
                raise Error(422, "listing snapshot byte budget exceeded")
            with catalog.transaction(True) as db:
                catalog._owned_bucket(db, bucket, user)
                now = timestamp(catalog.clock)
                db.execute("DELETE FROM snapshots WHERE expires<=?", (now,))
                if (
                    db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
                    >= MAX_SNAPSHOTS
                ):
                    raise Error(422, "active snapshot budget exceeded")
                if (
                    db.execute(
                        "SELECT count(*) FROM snapshots WHERE user=?", (user,)
                    ).fetchone()[0]
                    >= MAX_OWNER_SNAPSHOTS
                ):
                    raise Error(422, "owner snapshot budget exceeded")
                db.execute(
                    """INSERT INTO snapshots(
                    id,user,bucket,prefix,versions,page_size,expires,rows,delimiter)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        snapshot,
                        user,
                        bucket,
                        prefix,
                        versions,
                        limit,
                        now + SNAPSHOT_TTL,
                        raw,
                        delimiter,
                    ),
                )
    page = entries[index : index + limit]
    next_token = (
        _encode(snapshot, index + limit, secret)
        if index + limit < len(entries)
        else None
    )
    return {
        "objects": [item["value"] for item in page if item["kind"] == "object"],
        "common_prefixes": [item["value"] for item in page if item["kind"] == "prefix"],
        "continuation": next_token,
        "snapshot": snapshot,
        "snapshot_ttl_seconds": SNAPSHOT_TTL,
    }
