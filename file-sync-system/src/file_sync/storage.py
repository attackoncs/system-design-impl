"""Compressed, authenticated encrypted blocks; explicit local cold-tier demo."""

import hashlib
import hmac
import os
from pathlib import Path
import re
import uuid
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class CorruptBlock(OSError):
    pass


class ArchivedBlock(OSError):
    pass


class BlockStore:
    def __init__(self, root, master, block_size=4 * 1024**2):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cold = self.root / "cold"
        self.cold.mkdir(exist_ok=True)
        self.master, self.block_size = master, block_size

    def key(self, space, digest):
        if not isinstance(space, str) or not re.fullmatch(r"[0-9a-f]{32}", space):
            raise ValueError("invalid namespace")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid digest")
        return hmac.new(
            self.master, (space + digest).encode(), hashlib.sha256
        ).hexdigest()

    def path(self, key):
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid object key")
        return self.root / key

    def cipher(self, space):
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

    def put(self, space, digest, data):
        if (
            not isinstance(data, bytes)
            or not 0 < len(data) <= self.block_size
            or hashlib.sha256(data).hexdigest() != digest
        ):
            raise ValueError("block size/checksum mismatch")
        key = self.key(space, digest)
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
            with temporary.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(key))
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def get(self, space, digest, size):
        key = self.key(space, digest)
        path = self.path(key)
        if not path.exists() and (self.cold / key).is_file():
            raise ArchivedBlock("block needs explicit restore")
        if path.stat().st_size > self.block_size + 65536:
            raise CorruptBlock("oversized ciphertext")
        encoded = path.read_bytes()
        try:
            if encoded[:3] != b"FS1":
                raise ValueError()
            compressed = self.cipher(space).decrypt(
                encoded[3:15], encoded[15:], self.aad(space, digest, size)
            )
            decoder = zlib.decompressobj()
            data = decoder.decompress(compressed, self.block_size + 1)
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

    def exists(self, key):
        return self.path(key).is_file() or (self.cold / key).is_file()

    def archive(self, key):
        os.replace(self.path(key), self.cold / key)

    def restore(self, key):
        os.replace(self.cold / key, self.path(key))
