"""Atomic, content-addressed local object adapter."""
import hashlib
import os
from pathlib import Path
import re
import uuid


class LocalObjects:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def file(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid object key")
        return self.root / key

    def write(self, chunks):
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("wb") as stream:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            key = digest.hexdigest()
            os.replace(temporary, self.file(key))
            return key, size
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, value):
        return self.write([value])[0]

    def put_file(self, path):
        def chunks():
            with open(path, "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        return self.write(chunks())[0]

    def assemble(self, keys):
        def chunks():
            for key in keys:
                with self.file(key).open("rb") as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
        return self.write(chunks())
