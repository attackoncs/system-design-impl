"""Exclusive checkpoint ownership and atomic, crash-recoverable file writes."""

from contextlib import contextmanager
import os
from pathlib import Path
import uuid

from .common import Error, dumps, integer, text
from .records import MAX_POSITION, strict_json


def same_file(left, right):
    if Path(left).resolve() == Path(right).resolve():
        return True
    try:
        return Path(left).samefile(right)
    except FileNotFoundError:
        return False


@contextmanager
def checkpoint_lock(path):
    """OS advisory lock releases on process exit; the small sidecar remains."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Error(409, "checkpoint already has an active owner") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Error(409, "checkpoint already has an active owner") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_checkpoint(path):
    import hashlib
    import re

    try:
        with Path(path).open("rb") as source:
            raw = source.read(4097)
    except FileNotFoundError:
        return {"offset": 0, "prefix": hashlib.sha256(b"").hexdigest()}
    if len(raw) > 4096:
        raise ValueError("checkpoint exceeds 4096 bytes")
    body = strict_json(raw)
    if (
        not isinstance(body, dict)
        or not {"offset", "prefix"} <= set(body)
        or set(body) - {"offset", "prefix", "lineage"}
    ):
        raise ValueError("invalid checkpoint fields")
    integer(body["offset"], "checkpoint offset", 0, MAX_POSITION)
    if (
        not isinstance(body["prefix"], str)
        or re.fullmatch(r"[0-9a-f]{64}", body["prefix"]) is None
    ):
        raise ValueError("invalid checkpoint hash")
    if "lineage" in body:
        text(body["lineage"], "lineage")
    return body


def write_checkpoint(path, body):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    created = False
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(dumps(body))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if created:
            temporary.unlink(missing_ok=True)
