"""Checksummed partition segments, with one process owning each partition root.

The index is rebuildable. Failed disk updates make this instance unavailable
until reopening, instead of appending behind a stale in-memory tail.
"""

from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import threading
import uuid
import zlib

from .common import Error, ZERO, dumps, envelope, integer

HEADER = struct.Struct(">4sII")
INDEX = struct.Struct(">QQ")
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 24 * 1024 * 1024
MAX_BATCH_RECORDS = 10000


class Corruption(Error):
    def __init__(self, message):
        super().__init__(503, message)


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("invalid chain hash")
    return value


def _sync_directory(path):
    # Windows does not expose portable directory fsync through os.open.
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(dumps(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.lock:
            self._check_available()
            return method(self, *args, **kwargs)

    return guarded


def _value(payload):
    value = json.loads(payload)
    if not isinstance(value, dict) or set(value) != {"record", "previous", "hash"}:
        raise ValueError("invalid envelope")
    record = value["record"]
    if not isinstance(record, dict):
        raise ValueError("record object required")
    integer(record["offset"])
    timestamp = record["timestamp"]
    if type(timestamp) not in (float, int) or not math.isfinite(timestamp):
        raise ValueError("invalid record timestamp")
    _hash(value["previous"])
    _hash(value["hash"])
    if envelope(record, value["previous"]) != value:
        raise ValueError("invalid envelope/hash")
    return value


def _prepare(values, base, previous):
    """Own and validate the bounded batch before touching data files."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_BATCH_RECORDS:
        raise ValueError("record batch exceeds reference limit")
    prepared = []
    total = 0
    for source in values:
        payload = dumps(source).encode()
        total += HEADER.size + len(payload)
        if not 0 < len(payload) <= MAX_FRAME_BYTES or total > MAX_BATCH_BYTES:
            raise Error(413, "record batch exceeds reference byte limit")
        value = _value(payload)
        if value["record"]["offset"] != base or value["previous"] != previous:
            raise Error(409, "offset/hash append conflict")
        prepared.append((value, payload))
        base += 1
        previous = value["hash"]
    return prepared


class PartitionLog:
    def __init__(self, root, segment_bytes=1024 * 1024):
        self.root = Path(root)
        self.segment_bytes = integer(segment_bytes, 256, 16 * 1024 * 1024)
        self.lock = threading.RLock()
        self._failed = False
        self.root.mkdir(parents=True, exist_ok=True)
        pointer = self.root / "current.json"
        if not pointer.exists():
            generation = "generation-" + uuid.uuid4().hex
            directory = self.root / generation
            directory.mkdir()
            atomic_json(
                directory / "manifest.json",
                {"base": 0, "base_hash": ZERO, "epoch": 0, "committed": -1},
            )
            atomic_json(pointer, {"generation": generation})
        try:
            generation = json.loads(pointer.read_text(encoding="utf-8"))["generation"]
            if not isinstance(generation, str) or not re.fullmatch(
                r"generation-[0-9a-f]{32}", generation
            ):
                raise ValueError("invalid partition generation")
            self.directory = self.root / generation
            if self.directory.is_symlink():
                raise ValueError("partition generation must be a local directory")
            manifest = json.loads(
                (self.directory / "manifest.json").read_text(encoding="utf-8")
            )
            base = integer(manifest["base"])
            _hash(manifest["base_hash"])
            integer(manifest["epoch"])
            integer(manifest.setdefault("committed", base - 1), base - 1)
            self._manifest = manifest
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise Corruption("invalid partition manifest") from exc
        self._scan()
        self._old_generations()

    def _check_available(self):
        if self._failed:
            raise Corruption("partition requires reopening after a failed disk update")

    @property
    def manifest(self):
        with self.lock:
            return dict(self._manifest)

    @property
    def base(self):
        with self.lock:
            return self._manifest["base"]

    @property
    def next(self):
        with self.lock:
            self._check_available()
            return self._next_offset

    @_serialized
    def scan(self):
        try:
            self._scan()
        except (OSError, Corruption):
            self._failed = True
            raise

    def _scan(self):
        entries = {}
        expected = self.base
        previous = self._manifest["base_hash"]
        segments = sorted(self.directory.glob("*.log"))
        for path in segments:
            if not re.fullmatch(r"[0-9]{20}\.log", path.name) or path.is_symlink():
                raise Corruption("invalid segment path")
            index = []
            with path.open("r+b") as source:
                while True:
                    position = source.tell()
                    header = source.read(HEADER.size)
                    if not header:
                        break
                    if len(header) != HEADER.size:
                        self._recover_tail(source, path, segments[-1], position)
                        break
                    magic, size, crc = HEADER.unpack(header)
                    if magic != b"MQ01" or not 0 < size <= MAX_FRAME_BYTES:
                        raise Corruption("invalid log frame header")
                    payload = source.read(size)
                    if len(payload) != size:
                        self._recover_tail(source, path, segments[-1], position)
                        break
                    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                        raise Corruption("log CRC mismatch")
                    try:
                        value = _value(payload)
                        offset = value["record"]["offset"]
                    except (ValueError, KeyError, TypeError, OverflowError) as exc:
                        raise Corruption("invalid envelope/hash") from exc
                    index.append((offset, position))
                    if offset < self.base:
                        continue
                    if offset != expected or value["previous"] != previous:
                        raise Corruption("noncontiguous offset/hash chain")
                    entries[offset] = self._entry(path, position, size, value)
                    expected += 1
                    previous = value["hash"]
            index_path = path.with_suffix(".idx")
            index_payload = b"".join(INDEX.pack(*row) for row in index)
            if not index_path.exists() or index_path.read_bytes() != index_payload:
                with index_path.open("wb") as output:
                    output.write(index_payload)
                    output.flush()
                    os.fsync(output.fileno())
        if self._manifest["committed"] >= expected:
            raise Corruption("committed prefix was truncated")
        self.entries = entries
        self._next_offset = expected

    @staticmethod
    def _recover_tail(source, path, active, position):
        if path != active:
            raise Corruption("torn non-active segment")
        source.truncate(position)
        source.flush()
        os.fsync(source.fileno())

    @staticmethod
    def _entry(path, position, size, value):
        return {
            "path": path,
            "position": position,
            "size": HEADER.size + size,
            "hash": value["hash"],
            "timestamp": value["record"]["timestamp"],
        }

    @_serialized
    def hash_at(self, offset):
        integer(offset, -1)
        if offset == self.base - 1:
            return self._manifest["base_hash"]
        entry = self.entries.get(offset)
        return entry["hash"] if entry else None

    @_serialized
    def read(self, offset, limit=1000):
        integer(offset)
        integer(limit, 1, MAX_BATCH_RECORDS)
        if offset < self.base:
            raise Error(410, "offset expired by retention")
        result = []
        total = 0
        source = None
        active_path = None
        try:
            for number in range(offset, min(self.next, offset + limit)):
                entry = self.entries[number]
                if total + entry["size"] > MAX_BATCH_BYTES:
                    break
                if entry["path"] != active_path:
                    if source:
                        source.close()
                    active_path = entry["path"]
                    source = active_path.open("rb")
                source.seek(entry["position"])
                header = source.read(HEADER.size)
                if len(header) != HEADER.size:
                    raise Corruption("log truncated after indexing")
                magic, size, crc = HEADER.unpack(header)
                if (
                    magic != b"MQ01"
                    or not 0 < size <= MAX_FRAME_BYTES
                    or HEADER.size + size != entry["size"]
                ):
                    raise Corruption("log frame changed after indexing")
                payload = source.read(size)
                if len(payload) != size or zlib.crc32(payload) & 0xFFFFFFFF != crc:
                    raise Corruption("log changed/corrupted after indexing")
                try:
                    value = _value(payload)
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    raise Corruption("invalid envelope after indexing") from exc
                if value["hash"] != entry["hash"]:
                    raise Corruption("log hash mismatch after indexing")
                result.append(value)
                total += entry["size"]
        except (OSError, Corruption):
            self._failed = True
            raise
        finally:
            if source:
                source.close()
        return result

    @_serialized
    def append(self, values):
        prepared = _prepare(values, self.next, self.hash_at(self.next - 1))
        self._append_prepared(prepared)

    @_serialized
    def synchronize(self, epoch, committed, committed_hash, base, base_hash, values):
        integer(committed, -1)
        integer(base)
        _hash(committed_hash)
        _hash(base_hash)
        prepared = _prepare(values, committed + 1, committed_hash)
        if (
            not self.base <= base <= committed + 1
            or self.hash_at(base - 1) != base_hash
        ):
            raise Error(409, "invalid sync retention checkpoint")
        # All incoming records/checkpoints are validated before truncating a
        # provisional suffix. A malformed sync cannot destroy an earlier retry.
        self.align(epoch, committed, committed_hash)
        if base > self.base:
            self.retain(base, base_hash)
        self._append_prepared(prepared)

    def _append_prepared(self, prepared):
        if not prepared:
            return
        paths = sorted(self.directory.glob("*.log"))
        path = paths[-1] if paths else None
        position = path.stat().st_size if path else 0
        groups = []
        for value, payload in prepared:
            frame = HEADER.pack(b"MQ01", len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
            frame += payload
            if path is None or position + len(frame) > self.segment_bytes:
                path = self.directory / f"{value['record']['offset']:020}.log"
                position = 0
            if not groups or groups[-1][0] != path:
                groups.append((path, []))
            groups[-1][1].append((position, value, frame))
            position += len(frame)
        entries = {}
        try:
            for path, rows in groups:
                with path.open("ab") as output:
                    output.write(b"".join(row[2] for row in rows))
                    output.flush()
                    os.fsync(output.fileno())
                with path.with_suffix(".idx").open("ab") as index:
                    index.write(
                        b"".join(
                            INDEX.pack(row[1]["record"]["offset"], row[0])
                            for row in rows
                        )
                    )
                    index.flush()
                    os.fsync(index.fileno())
                for position, value, frame in rows:
                    entries[value["record"]["offset"]] = self._entry(
                        path, position, len(frame) - HEADER.size, value
                    )
            _sync_directory(self.directory)
        except OSError:
            self._failed = True
            raise
        self.entries.update(entries)
        self._next_offset += len(prepared)

    @_serialized
    def align(self, epoch, committed, committed_hash):
        integer(epoch)
        integer(committed, -1)
        _hash(committed_hash)
        if epoch < self._manifest["epoch"]:
            raise Error(409, "stale leader epoch")
        if committed < self._manifest["committed"]:
            raise Error(409, "committed checkpoint cannot move backwards")
        if self.hash_at(committed) != committed_hash:
            raise Error(409, "committed prefix mismatch; snapshot catchup required")
        manifest = {**self._manifest, "epoch": epoch, "committed": committed}
        # Persist fencing before destructive truncation. A crash can leave a
        # removable suffix, but cannot make an older epoch writable again.
        try:
            atomic_json(self.directory / "manifest.json", manifest)
            self._manifest = manifest
            cutoff = committed + 1
            if cutoff < self.next:
                entry = self.entries[cutoff]
                for path in sorted(self.directory.glob("*.log")):
                    if path > entry["path"]:
                        path.unlink()
                        path.with_suffix(".idx").unlink(missing_ok=True)
                with entry["path"].open("r+b") as output:
                    output.truncate(entry["position"])
                    output.flush()
                    os.fsync(output.fileno())
            _sync_directory(self.directory)
            self._scan()
        except (OSError, Corruption):
            self._failed = True
            raise

    @_serialized
    def install(
        self, values, base, base_hash, epoch, committed=None, committed_hash=None
    ):
        integer(base)
        integer(epoch)
        _hash(base_hash)
        if epoch < self._manifest["epoch"]:
            raise Error(409, "stale install epoch")
        prepared = _prepare(values, base, base_hash)
        checkpoint = base - 1 if committed is None else integer(committed, base - 1)
        hashes = {base - 1: base_hash}
        hashes.update(
            (value["record"]["offset"], value["hash"]) for value, _ in prepared
        )
        if checkpoint not in hashes:
            raise Error(409, "snapshot does not contain committed checkpoint")
        if committed_hash is not None and _hash(committed_hash) != hashes[checkpoint]:
            raise Error(409, "snapshot committed hash mismatch")
        observed = self._manifest["committed"]
        if checkpoint < observed:
            raise Error(409, "snapshot cannot roll back committed checkpoint")
        if observed in hashes and hashes[observed] != self.hash_at(observed):
            raise Error(409, "snapshot changes observed committed history")
        generation = "generation-" + uuid.uuid4().hex
        directory = self.root / generation
        directory.mkdir()
        published = False
        try:
            manifest = {
                "base": base,
                "base_hash": base_hash,
                "epoch": epoch,
                "committed": checkpoint,
            }
            atomic_json(directory / "manifest.json", manifest)
            temporary = object.__new__(PartitionLog)
            temporary.root, temporary.directory, temporary.segment_bytes = (
                self.root,
                directory,
                self.segment_bytes,
            )
            temporary._manifest = manifest
            temporary.lock = threading.RLock()
            temporary._failed = False
            temporary.entries = {}
            temporary._next_offset = base
            temporary._append_prepared(prepared)
            atomic_json(self.root / "current.json", {"generation": generation})
            published = True
            self.directory, self._manifest, self.entries, self._next_offset = (
                directory,
                manifest,
                temporary.entries,
                temporary._next_offset,
            )
        except OSError:
            # Pointer replacement may succeed before directory fsync fails.
            # Reopening selects the authoritative generation in that case.
            self._failed = True
            raise
        finally:
            if not published and not self._failed:
                self._remove_generation(directory)
        self._old_generations()

    def _remove_generation(self, directory):
        root = self.root.resolve()
        if (
            directory.parent.resolve() == root
            and directory.resolve().parent == root
            and re.fullmatch(r"generation-[0-9a-f]{32}", directory.name)
            and not directory.is_symlink()
        ):
            shutil.rmtree(directory)

    def _old_generations(self):
        # Cleanup happens after publication; a failed cleanup must not turn a
        # successful install into an ambiguous write outcome.
        for directory in self.root.glob("generation-*"):
            if directory != self.directory and directory.is_dir():
                try:
                    self._remove_generation(directory)
                except OSError:
                    pass

    @_serialized
    def retain(self, base, base_hash):
        integer(base)
        _hash(base_hash)
        if not self.base <= base <= self.next or self.hash_at(base - 1) != base_hash:
            raise Error(409, "invalid retention checkpoint")
        manifest = {
            **self._manifest,
            "base": base,
            "base_hash": base_hash,
            "committed": max(self._manifest["committed"], base - 1),
        }
        try:
            atomic_json(self.directory / "manifest.json", manifest)
            self._manifest = manifest
            paths = sorted(self.directory.glob("*.log"))
            needed = {
                entry["path"]
                for number, entry in self.entries.items()
                if number >= base
            }
            for path in paths[:-1]:
                if path not in needed:
                    path.unlink()
                    path.with_suffix(".idx").unlink(missing_ok=True)
            _sync_directory(self.directory)
            self._scan()
        except (OSError, Corruption):
            self._failed = True
            raise
        self._old_generations()
