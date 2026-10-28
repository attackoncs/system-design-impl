import base64
import hashlib
import os
from pathlib import Path
import re
import threading
import uuid

from .common import Error, http_json

MAX_OBJECT_BYTES = 8 * 1024 * 1024


def object_key(value):
    """Validate object identifiers before using them in paths or remote URLs."""
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("invalid SHA256 object key")
    return value


class ObjectStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._publish_locks = [threading.Lock() for _ in range(64)]

    def path(self, key):
        object_key(key)
        return self.root / key[:2] / key

    def put(self, data):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise ValueError("object exceeds 8 MiB")
        key = hashlib.sha256(data).hexdigest()
        # One object service owns this directory. Fixed lock stripes avoid both
        # unbounded per-key locks and Windows replacement races between uploads.
        with self._publish_locks[int(key[:2], 16) % len(self._publish_locks)]:
            return self._publish(key, data)

    def _publish(self, key, data):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.get(key)
            return {"key": key, "size": len(data)}
        tmp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        return {"key": key, "size": len(data)}

    def get(self, key):
        try:
            with self.path(key).open("rb") as handle:
                data = handle.read(MAX_OBJECT_BYTES + 1)
        except FileNotFoundError:
            raise Error(404, "object missing") from None
        if len(data) > MAX_OBJECT_BYTES or hashlib.sha256(data).hexdigest() != key:
            raise Error(409, "object integrity check failed")
        return data


class Objects:
    def __init__(self, config):
        self.url, self.token = config["object_url"], config["internal_token"]

    def put(self, data, *, timeout=5, deadline=None, cancel=None):
        if not isinstance(data, bytes) or len(data) > MAX_OBJECT_BYTES:
            raise ValueError("object exceeds 8 MiB")
        expected = {"key": hashlib.sha256(data).hexdigest(), "size": len(data)}
        result = http_json(
            self.url + "/objects",
            self.token,
            {"data": base64.b64encode(data).decode()},
            timeout=timeout,
            deadline=deadline,
            cancel=cancel,
        )
        if result != expected or type(result.get("size")) is not int:
            raise Error(502, "remote object receipt mismatch")
        return expected

    def get(self, key, *, timeout=5, deadline=None, cancel=None):
        object_key(key)
        result = http_json(
            self.url + "/objects/" + key,
            self.token,
            timeout=timeout,
            deadline=deadline,
            cancel=cancel,
        )
        if not isinstance(result, dict) or set(result) != {"data", "size"}:
            raise Error(502, "invalid remote object response")
        if (
            not isinstance(result["data"], str)
            or len(result["data"]) > ((MAX_OBJECT_BYTES + 2) // 3) * 4
        ):
            raise Error(502, "invalid remote object data")
        try:
            data = base64.b64decode(result["data"], validate=True)
        except ValueError:
            raise Error(502, "invalid remote object encoding") from None
        if (
            type(result["size"]) is not int
            or result["size"] != len(data)
            or len(data) > MAX_OBJECT_BYTES
        ):
            raise Error(502, "remote object length mismatch")
        if hashlib.sha256(data).hexdigest() != key:
            raise Error(409, "remote object checksum mismatch")
        return data
