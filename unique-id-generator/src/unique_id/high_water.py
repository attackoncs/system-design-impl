"""Durable restart high-water mark for one Snowflake node identity."""

from __future__ import annotations

import os
import tempfile
from typing import Optional, Union

DEFAULT_WINDOW_MS = 1000


class HighWaterMark:
    """File-backed timestamp ceiling that survives process restarts.

    The generator persists ``timestamp + window_ms`` before issuing any ID whose
    timestamp exceeds the current durable ceiling, so writes happen at most
    once per window. After a restart, IDs are issued only once the clock passes
    the stored ceiling, which is at least every previously issued timestamp.

    One file belongs to exactly one (datacenter_id, machine_id) identity and
    one generator instance at a time; this class does not lease identities.
    """

    def __init__(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        window_ms: int = DEFAULT_WINDOW_MS,
    ) -> None:
        if type(window_ms) is not int or window_ms <= 0:
            raise ValueError("window_ms must be a positive integer")
        self._path = os.fspath(path)
        self._window_ms = window_ms

    @property
    def path(self) -> str:
        """Location of the ceiling file."""
        return self._path

    @property
    def window_ms(self) -> int:
        """How far ahead of the issuing timestamp each reservation reaches."""
        return self._window_ms

    def load(self) -> Optional[int]:
        """Return the persisted ceiling in Unix ms, or None on first start.

        Raises:
            ValueError: If the file exists but does not hold a nonnegative
                integer. Corruption is never treated as "no history".
        """
        try:
            with open(self._path, encoding="ascii") as handle:
                text = handle.read().strip()
        except FileNotFoundError:
            return None
        if not text.isdigit():
            raise ValueError(f"corrupt high-water file {self._path!r}: {text!r}")
        return int(text)

    def store(self, ceiling_ms: int) -> None:
        """Durably replace the ceiling: temp file, fsync, rename, fsync dir."""
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".high-water-")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(f"{ceiling_ms}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        _fsync_directory(directory)


def _fsync_directory(directory: str) -> None:
    """Make the rename durable where the platform supports directory fsync."""
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
