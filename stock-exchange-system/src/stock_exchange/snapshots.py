"""Validated legacy snapshot upgrade and serialized atomic publication."""

import copy
import os
from pathlib import Path
import tempfile
import time

from .common import checksum, encode


def compatible(actual, expected):
    if actual == expected:
        return True
    if any(
        key in actual
        for key in ("book_index", "execution_seq", "last_seq", "last_timestamp")
    ):
        return False
    legacy = copy.deepcopy(expected)
    for key in ("book_index", "execution_seq", "last_seq", "last_timestamp"):
        legacy.pop(key)
    for trade in legacy["trades"]:
        trade.pop("execution_seq")
    return actual == legacy


def publish(path, payload):
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encode({"payload": payload, "checksum": checksum(payload)}))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)
