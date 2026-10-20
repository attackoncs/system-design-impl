import base64
import hashlib
import os
from pathlib import Path
import re
import uuid

from .common import Error, http_json


class ObjectStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("invalid SHA256 object key")
        return self.root/key[:2]/key

    def put(self, data):
        if not isinstance(data, bytes) or len(data)>8*1024*1024:
            raise ValueError("object exceeds 8 MiB")
        key = hashlib.sha256(data).hexdigest()
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.get(key)
            return {"key": key, "size": len(data)}
        tmp = path.with_suffix("."+uuid.uuid4().hex+".tmp")
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
            data = self.path(key).read_bytes()
        except FileNotFoundError:
            raise Error(404, "object missing") from None
        if hashlib.sha256(data).hexdigest()!=key:
            raise Error(409, "object integrity check failed")
        return data


class Objects:
    def __init__(self, config):
        self.url, self.token = config["object_url"], config["internal_token"]

    def put(self, data):
        return http_json(self.url+"/objects", self.token, {"data": base64.b64encode(data).decode()})

    def get(self, key):
        result = http_json(self.url+"/objects/"+key, self.token)
        data = base64.b64decode(result["data"], validate=True)
        if hashlib.sha256(data).hexdigest()!=key:
            raise Error(409, "remote object checksum mismatch")
        return data
