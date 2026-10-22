"""Private S3 objects with per-host, checksum-verified materialization."""

import hashlib
import os
import re
import types
import uuid

import video_streaming.storage

try:
    import boto3
    import botocore.config
except ImportError:
    boto3 = None  # Optional S3 backend.
    botocore = None


class RemoteObject:
    """Read-only path-like access to an immutable remote content-addressed
    object.
    """

    def __init__(self, store, key):
        self.store, self.name = store, key

    def stat(self):
        return types.SimpleNamespace(
            st_size=self.store.head(self.name)["ContentLength"]
        )

    def is_file(self):
        """Return whether the object exists remotely; propagate service
        failures.
        """
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


class S3Objects(video_streaming.storage.LocalObjects):
    """A private S3 object store with independent local materialization
    caches.
    """

    def __init__(
        self,
        root,
        bucket,
        endpoint=None,
        prefix="video/",
        region="us-east-1",
        client=None,
    ):
        super().__init__(root)
        if (
            not bucket
            or not prefix
            or prefix.startswith("/")
            or ".." in prefix.split("/")
        ):
            raise ValueError(
                "private bucket and nonempty isolated prefix required"
            )
        self.bucket, self.prefix = bucket, prefix.rstrip("/") + "/"
        if client is None:
            if boto3 is None or botocore is None:
                raise ImportError("Install the distributed extra")

            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                config=botocore.config.Config(
                    connect_timeout=3,
                    read_timeout=15,
                    retries={"max_attempts": 2},
                    s3={"addressing_style": "path"},
                ),
            )
        self.client = client

    def file(self, key):
        video_streaming.storage.LocalObjects.file(
            self, key
        )  # Key validation, no untrusted path components.
        return RemoteObject(self, key)

    def head(self, key):
        """Return remote object metadata, raising FileNotFoundError if
        absent.
        """
        video_streaming.storage.LocalObjects.file(self, key)
        try:
            return self.client.head_object(
                Bucket=self.bucket, Key=self.prefix + key
            )
        except Exception as error:
            response = getattr(error, "response", {})
            if response.get("Error", {}).get("Code") in (
                "404",
                "NoSuchKey",
                "NotFound",
            ):
                raise FileNotFoundError("remote object missing") from None
            raise OSError("object storage unavailable") from None

    def materialize(self, key):
        """Verify remote presence and return a checksum-validated local copy.

        Args:
            key: SHA-256 object key to materialize.

        Returns:
            The local cache path after successful remote existence verification.

        Raises:
            FileNotFoundError: The remote object is missing.
            OSError: Remote access or checksum validation fails.
        """
        target = video_streaming.storage.LocalObjects.file(self, key)
        # Always verify remote presence; a local copy is not remote durability.
        self.head(key)
        if target.is_file():
            return target
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        try:
            try:
                self.client.download_file(
                    self.bucket, self.prefix + key, str(temporary)
                )
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
        # Write local staging without dispatching LocalObjects.file to remote
        # proxy.
        """Upload byte chunks durably to S3 and return (object key, byte
        count).
        """
        local = video_streaming.storage.LocalObjects(self.root)
        key, size = local.write(chunks)
        try:
            self.client.upload_file(
                str(local.file(key)),
                self.bucket,
                self.prefix + key,
                ExtraArgs={"Metadata": {"sha256": key}},
            )
        except Exception:
            raise OSError("object upload unavailable") from None
        return key, size

    def sweep(self, references, cutoff):
        """Remove old unreferenced objects inside this store's configured
        prefix.

        Args:
            references: Content-addressed object keys that must be retained.
            cutoff: Exclusive object modification cutoff in epoch seconds.

        Returns:
            The number of removed objects; other prefixes are never considered.
        """
        removed = 0
        try:
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix
            )
            for page in pages:
                for item in page.get("Contents", []):
                    key = item["Key"][len(self.prefix) :]
                    if (
                        re.fullmatch(r"[0-9a-f]{64}", key)
                        and key not in references
                        and item["LastModified"].timestamp() < cutoff
                    ):
                        self.client.delete_object(
                            Bucket=self.bucket, Key=item["Key"]
                        )
                        removed += 1
        except Exception:
            raise OSError("object sweep unavailable") from None
        return removed
