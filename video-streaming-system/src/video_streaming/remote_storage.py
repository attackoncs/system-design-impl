"""Private S3 objects with per-host, checksum-verified materialization."""
import hashlib
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from .storage import LocalObjects


class RemoteObject:
    def __init__(self, store, key):
        self.store, self.name = store, key

    def stat(self):
        return SimpleNamespace(st_size=self.store.head(self.name)["ContentLength"])

    def is_file(self):
        try:
            self.store.head(self.name)
            return True
        except FileNotFoundError:
            return False

    def read_bytes(self):
        return self.store.materialize(self.name).read_bytes()

    def open(self, mode="rb"):
        if mode != "rb":
            raise ValueError("remote objects are immutable")
        return self.store.materialize(self.name).open(mode)

    def __fspath__(self):
        return str(self.store.materialize(self.name))


class S3Objects(LocalObjects):
    def __init__(self, root, bucket, endpoint=None, prefix="video/", region="us-east-1", client=None):
        super().__init__(root)
        if not bucket or not prefix or prefix.startswith("/") or ".." in prefix.split("/"):
            raise ValueError("private bucket and nonempty isolated prefix required")
        self.bucket, self.prefix = bucket, prefix.rstrip("/") + "/"
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                config=Config(connect_timeout=3, read_timeout=15,
                              retries={"max_attempts": 2}, s3={"addressing_style": "path"}))
        self.client = client

    def file(self, key):
        LocalObjects.file(self, key)  # Key validation, no untrusted path components.
        return RemoteObject(self, key)

    def head(self, key):
        LocalObjects.file(self, key)
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self.prefix + key)
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError("remote object missing") from None
            raise OSError("object storage unavailable") from None

    def materialize(self, key):
        target = LocalObjects.file(self, key)
        # Always verify remote presence; a local copy is not remote durability.
        self.head(key)
        if target.is_file():
            return target
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        try:
            try:
                self.client.download_file(self.bucket, self.prefix + key, str(temporary))
            except Exception:
                raise OSError("object download unavailable") from None
            digest = hashlib.sha256()
            with temporary.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != key:
                raise OSError("object checksum mismatch")
            os.replace(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    def write(self, chunks):
        # Write local staging without dispatching LocalObjects.file to remote proxy.
        local = LocalObjects(self.root)
        key, size = local.write(chunks)
        try:
            self.client.upload_file(str(local.file(key)), self.bucket, self.prefix + key,
                                    ExtraArgs={"Metadata": {"sha256": key}})
        except Exception:
            raise OSError("object upload unavailable") from None
        return key, size

    def sweep(self, references, cutoff):
        removed = 0
        try:
            pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=self.prefix)
            for page in pages:
                for item in page.get("Contents", []):
                    key = item["Key"][len(self.prefix):]
                    if re.fullmatch(r"[0-9a-f]{64}", key) and key not in references and item["LastModified"].timestamp() < cutoff:
                        self.client.delete_object(Bucket=self.bucket, Key=item["Key"])
                        removed += 1
        except Exception:
            raise OSError("object sweep unavailable") from None
        return removed
