"""Bounded authenticated block storage and coordinated local hot/cold tiers."""

from contextlib import contextmanager
import errno
import hashlib
import hmac
import os
from pathlib import Path
import re
import threading
import time
import uuid
import weakref
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class CorruptBlock(OSError):
    """Stored ciphertext, compressed stream or plaintext does not match metadata."""


class ArchivedBlock(OSError):
    """The block exists only in the explicitly managed cold tier."""


def _hex(value, length, label):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None
    ):
        raise ValueError(f"invalid {label}")
    return value


def _sync_directory(path):
    # Windows cannot open a directory this way. File fsync still precedes publish.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _Guards:
    def __init__(self):
        self.locks = tuple(threading.RLock() for _ in range(64))
        self.local = threading.local()


_guard_lock = threading.Lock()
_guard_sets = weakref.WeakValueDictionary()


class BlockStore:
    """Immutable FS1 blocks, with fixed lock stripes coordinating local processes.

    The object directory and its lock files must be shared on one local host.
    This is not a lock service for network filesystems. Raw administrative edits
    bypass these guards and are outside the concurrent API contract.
    """

    def __init__(self, root, master, block_size=4 * 1024**2):
        if not isinstance(master, bytes) or len(master) != 32:
            raise ValueError("master must contain 32 bytes")
        if type(block_size) is not int or not 1 <= block_size <= 4 * 1024**2:
            raise ValueError("block size must be 1..4 MiB")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cold = self.root / "cold"
        self._lock_root = self.root / ".locks"
        for directory in (self.cold, self._lock_root):
            if directory.is_symlink():
                raise ValueError("storage subdirectories cannot be symlinks")
            directory.mkdir(exist_ok=True)
        self.master, self.block_size = master, block_size
        with _guard_lock:
            root_key = os.path.normcase(str(self.root))
            self._guards = _guard_sets.get(root_key)
            if self._guards is None:
                self._guards = _Guards()
                _guard_sets[root_key] = self._guards
        self._locks = self._guards.locks

    def key(self, space, digest):
        _hex(space, 32, "namespace")
        _hex(digest, 64, "digest")
        return hmac.new(
            self.master, (space + digest).encode(), hashlib.sha256
        ).hexdigest()

    def path(self, key):
        return self.root / _hex(key, 64, "object key")

    @contextmanager
    def locked(self, key):
        """Bounded stripes protect publication, tier moves and GC from each other."""
        _hex(key, 64, "object key")
        stripe = int(key[:2], 16) % len(self._locks)
        with self._locks[stripe]:
            held = getattr(self._guards.local, "held", None)
            if held is None:
                held = self._guards.local.held = set()
            if stripe in held:
                yield
                return
            with (self._lock_root / f"{stripe:02d}.lock").open("a+b") as lock:
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"0")
                    lock.flush()
                deadline = time.monotonic() + 10
                while True:
                    try:
                        lock.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as error:
                        if error.errno not in (
                            errno.EACCES,
                            errno.EAGAIN,
                            errno.EDEADLK,
                        ):
                            raise
                        if time.monotonic() >= deadline:
                            raise TimeoutError("block storage is busy; retry") from None
                        time.sleep(0.005)
                held.add(stripe)
                try:
                    yield
                finally:
                    held.remove(stripe)
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def cipher(self, space):
        _hex(space, 32, "namespace")
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=bytes.fromhex(space),
            info=b"file-sync-blocks-v1",
        ).derive(self.master)
        return AESGCM(key)

    @staticmethod
    def aad(space, digest, size):
        return f"{space}:{digest}:{size}".encode()

    def _read(self, path, space, digest, size):
        if path.is_symlink():
            raise CorruptBlock("object cannot be a symlink")
        # Bound the actual read, not only a racy stat result.
        maximum = self.block_size + 65536
        with path.open("rb") as stream:
            encoded = stream.read(maximum + 1)
        if not 31 <= len(encoded) <= maximum:
            raise CorruptBlock("invalid ciphertext size")
        try:
            if encoded[:3] != b"FS1":
                raise ValueError()
            compressed = self.cipher(space).decrypt(
                encoded[3:15], encoded[15:], self.aad(space, digest, size)
            )
            decoder = zlib.decompressobj()
            data = decoder.decompress(compressed, size + 1)
            if (
                len(data) != size
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
                or hashlib.sha256(data).hexdigest() != digest
            ):
                raise ValueError()
            return data
        except (ValueError, InvalidTag, zlib.error):
            raise CorruptBlock("block integrity failure") from None

    def _reuse(self, path, space, digest, data):
        try:
            if self._read(path, space, digest, len(data)) != data:
                return False
            # The authenticated read rejected symlinks while holding the guard.
            # Windows does not implement utime(follow_symlinks=False).
            os.utime(path, None)
            return True
        except (FileNotFoundError, CorruptBlock):
            return False

    def put(self, space, digest, data):
        key = self.key(space, digest)
        if (
            not isinstance(data, bytes)
            or not 0 < len(data) <= self.block_size
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("block size/checksum mismatch")
        with self.locked(key):
            hot, cold = self.path(key), self.cold / key
            if self._reuse(hot, space, digest, data):
                return key
            if not hot.exists() and self._reuse(cold, space, digest, data):
                os.replace(cold, hot)
                _sync_directory(self.root)
                _sync_directory(self.cold)
                return key
            nonce = os.urandom(12)
            encoded = (
                b"FS1"
                + nonce
                + self.cipher(space).encrypt(
                    nonce, zlib.compress(data), self.aad(space, digest, len(data))
                )
            )
            temporary = self.root / (uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, hot)
                _sync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)
            return key

    def get(self, space, digest, size):
        key = self.key(space, digest)
        if type(size) is not int or not 1 <= size <= self.block_size:
            raise ValueError("invalid block size")
        with self.locked(key):
            hot = self.path(key)
            if not hot.exists() and (self.cold / key).is_file():
                raise ArchivedBlock("block needs explicit restore")
            return self._read(hot, space, digest, size)

    def exists(self, key):
        with self.locked(key):
            return any(
                path.is_file() and not path.is_symlink()
                for path in (self.path(key), self.cold / key)
            )

    def _move(self, source, target):
        if not source.exists():
            if target.is_file() and not target.is_symlink():
                return  # Retrying an already completed move is idempotent.
            raise FileNotFoundError(source)
        if source.is_symlink() or target.is_symlink():
            raise CorruptBlock("tier object cannot be a symlink")
        if target.exists():
            # Do not destroy a potentially valuable copy when legacy/manual
            # operations left different ciphertext in both tiers.
            raise FileExistsError("both tiers contain this object; reconcile copies")
        os.replace(source, target)
        _sync_directory(source.parent)
        _sync_directory(target.parent)

    def archive(self, key):
        with self.locked(key):
            self._move(self.path(key), self.cold / key)

    def restore(self, key):
        with self.locked(key):
            self._move(self.cold / key, self.path(key))

    def remove(self, key):
        """Remove both tiers under the same guard used by readers and publishers."""
        with self.locked(key):
            removed = 0
            for path in (self.path(key), self.cold / key):
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
            if removed:
                _sync_directory(self.root)
                _sync_directory(self.cold)
            return removed
