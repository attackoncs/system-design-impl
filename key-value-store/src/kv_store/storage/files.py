"""Publish immutable storage files only after their contents are durable."""

from __future__ import annotations

import errno
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


def sync_directory(path):
    # Windows does not expose portable directory fsync through Python. File
    # fsync and atomic replacement are still performed; document that limit.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


@contextmanager
def publishing(path, buffering=1024 * 1024):
    """Yield a writable stream whose contents become ``path`` only when durable.

    The stream is a new temporary file in the same directory. On a clean exit it
    is flushed and fsynced, atomically renamed over ``path`` and the directory
    is synced; on any error the temporary file is removed and nothing is
    published. Writers can therefore stream arbitrarily large files.
    """
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb", buffering=buffering) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def publish(path, payload):
    with publishing(path) as stream:
        stream.write(payload)
