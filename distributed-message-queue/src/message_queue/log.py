import json
import os
import shutil
from pathlib import Path
import struct
import uuid
import zlib

from .common import Error, ZERO, dumps, envelope, integer

HEADER = struct.Struct(">4sII")
INDEX = struct.Struct(">QQ")


class Corruption(Error):
    def __init__(self, message):
        super().__init__(503, message)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8") as output:
        output.write(dumps(value))
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class PartitionLog:
    def __init__(self, root, segment_bytes=1024 * 1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.segment_bytes = integer(segment_bytes, 256, 16 * 1024 * 1024)
        pointer = self.root / "current.json"
        if not pointer.exists():
            generation = "generation-" + uuid.uuid4().hex
            directory = self.root / generation
            directory.mkdir()
            atomic_json(
                directory / "manifest.json", {"base": 0, "base_hash": ZERO, "epoch": 0}
            )
            atomic_json(pointer, {"generation": generation})
        generation = json.loads(pointer.read_text())["generation"]
        if not generation.startswith("generation-") or not generation[11:].isalnum():
            raise Corruption("invalid partition generation")
        self.directory = self.root / generation
        self.manifest = json.loads((self.directory / "manifest.json").read_text())
        self.scan()

    @property
    def base(self):
        return self.manifest["base"]

    @property
    def next(self):
        return max(self.entries, default=self.base - 1) + 1

    def scan(self):
        self.entries = {}
        expected = self.base
        previous = self.manifest["base_hash"]
        segments = sorted(self.directory.glob("*.log"))
        for path in segments:
            index = []
            with path.open("r+b") as source:
                while True:
                    position = source.tell()
                    header = source.read(HEADER.size)
                    if not header:
                        break
                    if len(header) != HEADER.size:
                        if path != segments[-1]:
                            raise Corruption("torn non-active segment header")
                        source.truncate(position)
                        source.flush()
                        os.fsync(source.fileno())
                        break
                    magic, size, crc = HEADER.unpack(header)
                    if magic != b"MQ01" or size > 2 * 1024 * 1024:
                        raise Corruption("invalid log frame header")
                    payload = source.read(size)
                    if len(payload) != size:
                        if path != segments[-1]:
                            raise Corruption("torn non-active segment payload")
                        source.truncate(position)
                        source.flush()
                        os.fsync(source.fileno())
                        break
                    if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                        raise Corruption("log CRC mismatch")
                    try:
                        value = json.loads(payload)
                        record = value["record"]
                        offset = integer(record["offset"])
                        if envelope(record, value["previous"]) != value:
                            raise ValueError()
                    except (ValueError, KeyError, TypeError):
                        raise Corruption("invalid envelope/hash")
                    index.append((offset, position))
                    if offset < self.base:
                        continue
                    if offset != expected or value["previous"] != previous:
                        raise Corruption("noncontiguous offset/hash chain")
                    self.entries[offset] = {
                        "path": path,
                        "position": position,
                        "size": HEADER.size + size,
                        "hash": value["hash"],
                        "timestamp": record["timestamp"],
                    }
                    expected += 1
                    previous = value["hash"]
            with path.with_suffix(".idx").open("wb") as output:
                for offset, position in index:
                    output.write(INDEX.pack(offset, position))
                output.flush()
                os.fsync(output.fileno())

    def hash_at(self, offset):
        if offset == self.base - 1:
            return self.manifest["base_hash"]
        if offset not in self.entries:
            return None
        return self.entries[offset]["hash"]

    def read(self, offset, limit=1000):
        integer(offset)
        integer(limit, 1, 10000)
        if offset < self.base:
            raise Error(410, "offset expired by retention")
        result = []
        for number in range(offset, min(self.next, offset + limit)):
            entry = self.entries[number]
            with entry["path"].open("rb") as source:
                source.seek(entry["position"])
                header = source.read(HEADER.size)
                if len(header) != HEADER.size:
                    raise Corruption("log truncated after indexing")
                magic, size, crc = HEADER.unpack(header)
                payload = source.read(size)
                if (
                    magic != b"MQ01"
                    or size > 2 * 1024 * 1024
                    or len(payload) != size
                    or zlib.crc32(payload) & 0xFFFFFFFF != crc
                ):
                    raise Corruption("log changed/corrupted after indexing")
                value = json.loads(payload)
                if (
                    envelope(value["record"], value["previous"]) != value
                    or value["hash"] != entry["hash"]
                ):
                    raise Corruption("log hash mismatch after indexing")
                result.append(value)
        return result

    def append(self, values):
        previous = self.hash_at(self.next - 1)
        next_offset = self.next
        # Validate the complete batch before modifying disk.
        for value in values:
            if (
                value["record"]["offset"] != next_offset
                or value["previous"] != previous
                or envelope(value["record"], previous) != value
            ):
                raise Error(409, "offset/hash append conflict")
            next_offset += 1
            previous = value["hash"]
        for value in values:
            payload = dumps(value).encode()
            frame = (
                HEADER.pack(b"MQ01", len(payload), zlib.crc32(payload) & 0xFFFFFFFF)
                + payload
            )
            paths = sorted(self.directory.glob("*.log"))
            path = (
                paths[-1]
                if paths and paths[-1].stat().st_size + len(frame) <= self.segment_bytes
                else self.directory / f"{value['record']['offset']:020}.log"
            )
            with path.open("ab") as output:
                position = output.tell()
                output.write(frame)
                output.flush()
                os.fsync(output.fileno())
            with path.with_suffix(".idx").open("ab") as index:
                index.write(INDEX.pack(value["record"]["offset"], position))
                index.flush()
                os.fsync(index.fileno())
            self.entries[value["record"]["offset"]] = {
                "path": path,
                "position": position,
                "size": len(frame),
                "hash": value["hash"],
                "timestamp": value["record"]["timestamp"],
            }

    def align(self, epoch, committed, committed_hash):
        integer(epoch)
        if epoch < self.manifest["epoch"]:
            raise Error(409, "stale leader epoch")
        if self.hash_at(committed) != committed_hash:
            raise Error(409, "committed prefix mismatch; snapshot catchup required")
        cutoff = committed + 1
        paths = sorted(self.directory.glob("*.log"))
        if cutoff < self.next:
            entry = self.entries[cutoff]
            for path in paths:
                if path > entry["path"]:
                    path.unlink()
                    path.with_suffix(".idx").unlink(missing_ok=True)
            with entry["path"].open("r+b") as output:
                output.truncate(entry["position"])
                output.flush()
                os.fsync(output.fileno())
        self.manifest["epoch"] = epoch
        atomic_json(self.directory / "manifest.json", self.manifest)
        self.scan()

    def install(self, values, base, base_hash, epoch):
        if epoch < self.manifest["epoch"]:
            raise Error(409, "stale install epoch")
        generation = "generation-" + uuid.uuid4().hex
        directory = self.root / generation
        directory.mkdir()
        atomic_json(
            directory / "manifest.json",
            {"base": base, "base_hash": base_hash, "epoch": epoch},
        )
        temporary = object.__new__(PartitionLog)
        temporary.root, temporary.directory, temporary.segment_bytes = (
            self.root,
            directory,
            self.segment_bytes,
        )
        temporary.manifest = {"base": base, "base_hash": base_hash, "epoch": epoch}
        temporary.entries = {}
        temporary.append(values)
        atomic_json(self.root / "current.json", {"generation": generation})
        self.directory, self.manifest, self.entries = (
            directory,
            temporary.manifest,
            temporary.entries,
        )
        self._old_generations()

    def _old_generations(self):
        root = self.root.resolve()
        for directory in self.root.glob("generation-*"):
            if (
                directory != self.directory
                and directory.is_dir()
                and directory.resolve().parent == root
                and directory.name[11:].isalnum()
            ):
                shutil.rmtree(directory)

    def retain(self, base, base_hash):
        if not self.base <= base <= self.next or self.hash_at(base - 1) != base_hash:
            raise Error(409, "invalid retention checkpoint")
        self.manifest.update(base=base, base_hash=base_hash)
        atomic_json(self.directory / "manifest.json", self.manifest)
        paths = sorted(self.directory.glob("*.log"))
        for path in paths[:-1]:
            offsets = [
                number
                for number, entry in self.entries.items()
                if entry["path"] == path
            ]
            if not offsets or max(offsets) < base:
                path.unlink()
                path.with_suffix(".idx").unlink(missing_ok=True)
        self.scan()
        self._old_generations()
