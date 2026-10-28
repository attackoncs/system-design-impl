from .listing import list_objects
from .catalog import Catalog as Catalog
from .chunk_io import ChunkIO
from .metadata_records import manifest_copy, publication
from .service_runtime import Runtime
from functools import wraps
import hashlib
import json
import threading

from .common import (
    Error,
    checksum,
    dumps,
    identifier,
    integer,
    object_key,
    request,
    sha,
    shard_for,
)


def serialized_upload(method):
    @wraps(method)
    def call(self, upload, *args, **kwargs):
        identifier(upload)
        lock = self._upload_locks[
            int(sha(upload.encode())[:8], 16) % len(self._upload_locks)
        ]
        deadline, cancel = self.runtime.context()
        while not lock.acquire(
            timeout=min(0.05, self.runtime.remaining(deadline, cancel))
        ):
            pass
        try:
            return method(self, upload, *args, **kwargs)
        finally:
            lock.release()

    return call


class Service:
    def __init__(self, config, catalog):
        self.config, self.catalog = json.loads(dumps(config)), catalog
        self.runtime = Runtime()
        self._upload_locks = [threading.RLock() for _ in range(64)]
        self.chunks = ChunkIO(self.config, self.runtime, self._rpc)

    def operation(self, *, deadline, cancel=None):
        return self.runtime.operation(deadline=deadline, cancel=cancel)

    def guard(self, method, path):
        parts = path.strip("/").split("/")
        exclusive = (
            path == "/admin/maintenance"
            or (
                method == "GET"
                and len(parts) == 4
                and parts[:2] == ["v1", "buckets"]
                and parts[3] == "objects"
            )
            or (
                method == "DELETE"
                and len(parts) == 3
                and parts[:2] == ["v1", "buckets"]
            )
        )
        return self.runtime.guard(exclusive)

    def close(self):
        self.runtime.close()

    def _rpc(self, *args, **kwargs):
        deadline, cancel = self.runtime.context()
        kwargs.setdefault("deadline", deadline)
        kwargs.setdefault("cancel", cancel)
        return request(*args, **kwargs)

    def meta(self, bucket, key, path, body=None, method=None):
        index = shard_for(bucket, key, len(self.config["metadata_urls"]))
        return self._rpc(
            self.config["metadata_urls"][index] + path,
            self.config["internal_token"],
            body,
            method,
        )

    def publish(
        self, bucket, key, key_id, manifest, size, checksum_value, marker=False
    ):
        manifest = publication(
            bucket, key, key_id, manifest, size, checksum_value, marker
        )
        self.catalog.prepare_publication(
            bucket, key, key_id, manifest, size, checksum_value, marker
        )
        result = self.meta(
            bucket,
            key,
            "/publish",
            {
                "bucket": bucket,
                "key": key,
                "request": key_id,
                "manifest": manifest,
                "size": size,
                "sha": checksum_value,
                "marker": marker,
            },
        )
        if (
            not isinstance(result, dict)
            or set(result) != {"version", "duplicate"}
            or type(result["duplicate"]) is not bool
        ):
            raise Error(502, "invalid metadata publication receipt")
        try:
            identifier(result["version"])
        except ValueError:
            raise Error(502, "invalid metadata publication receipt") from None
        self.catalog.finish_publication(bucket, key, key_id)
        return result

    def write_chunk(self, data):
        return self.chunks.write(data)

    def store_bytes(self, data):
        return self.chunks.store(data)

    def read_chunk(self, key):
        return self.chunks.read(key)

    def put(self, bucket, key, key_id, data, expected):
        object_key(key)
        identifier(key_id)
        if not isinstance(data, bytes) or len(data) > 4 * 1024 * 1024:
            raise Error(413, "single upload requires at most 4 MiB immutable bytes")
        if sha(data) != checksum(expected):
            raise Error(400, "whole-object checksum mismatch")
        manifest = self.store_bytes(data)
        return self.publish(bucket, key, key_id, manifest, len(data), expected)

    def get(self, bucket, key, version=None):
        row = self.meta(
            bucket, key, "/get", {"bucket": bucket, "key": key, "version": version}
        )
        try:
            identifier(row["id"])
            if (
                row["bucket"] != bucket
                or row["key"] != key
                or (version is not None and row["id"] != version)
            ):
                raise ValueError("object identity mismatch")
            row["manifest"] = publication(
                bucket,
                key,
                "read",
                row["manifest"],
                row["size"],
                row["sha"],
                bool(row["marker"]),
            )
            if type(row["marker"]) is not int or row["marker"] not in (0, 1):
                raise ValueError("invalid marker")
        except (ValueError, KeyError, TypeError):
            raise Error(502, "invalid metadata object descriptor") from None
        if row["marker"]:
            raise Error(404, "latest version is a delete marker")
        data = b"".join(self.read_chunk(c["hash"]) for c in row["manifest"])
        if len(data) != row["size"] or sha(data) != row["sha"]:
            raise Error(409, "whole-object integrity mismatch")
        return row, data

    @serialized_upload
    def part(self, upload, user, number, data, expected):
        if len(data) > 1024 * 1024 or not data:
            raise Error(413, "part requires 1..1048576 bytes")
        if sha(data) != checksum(expected):
            raise Error(400, "part checksum mismatch")
        state = self.catalog.upload(upload, user)
        if state["state"] != "active" or state["expires"] <= self.catalog.clock():
            raise Error(409, "upload is not active")
        integer(number, "part number", 1, 64)
        return self.catalog.add_part(upload, user, number, data, self.store_bytes(data))

    @serialized_upload
    def complete(self, upload, user):
        state = self.catalog.upload(upload, user)
        if state["state"] == "complete":
            return json.loads(state["result"])
        if state["state"] == "active":
            if state["expires"] <= self.catalog.clock():
                raise Error(409, "upload expired")
            if [p["number"] for p in state["parts"]] != list(
                range(1, len(state["parts"]) + 1)
            ):
                raise Error(409, "part numbers must be contiguous from one")
            manifest = [chunk for part in state["parts"] for chunk in part["manifest"]]
            if len(manifest) > 256:
                raise Error(422, "manifest exceeds 256 chunks")
            whole = hashlib.sha256()
            size = 0
            for chunk in manifest:
                data = self.read_chunk(chunk["hash"])
                if len(data) != chunk["size"]:
                    raise Error(409, "chunk size mismatch")
                whole.update(data)
                size += len(data)
            if size != state["size"] or whole.hexdigest() != state["sha"]:
                raise Error(409, "completion size/checksum mismatch")
            # Freeze intent before cross-service publication. Expiry cleanup must
            # not abort this state; maintenance can safely resume it after a crash.
            with self.catalog.transaction(True) as db:
                fresh = self.catalog._upload(db, upload, user)
                if (
                    fresh["state"] != "active"
                    or fresh["expires"] <= self.catalog.clock()
                    or dumps(fresh["parts"]) != dumps(state["parts"])
                ):
                    raise Error(
                        409, "upload changed while validating completion; retry"
                    )
                db.execute(
                    "UPDATE uploads SET state='completing',manifest=? WHERE id=?",
                    (dumps(manifest), upload),
                )
            state["state"], state["manifest"] = "completing", dumps(manifest)
        if state["state"] != "completing":
            raise Error(409, "upload cannot complete")
        result = self.publish(
            state["bucket"],
            state["key"],
            state["request"],
            json.loads(state["manifest"]),
            state["size"],
            state["sha"],
        )
        with self.catalog.transaction(True) as db:
            db.execute(
                "UPDATE uploads SET state='complete',result=? WHERE id=?",
                (dumps(result), upload),
            )
        return result

    @serialized_upload
    def abort(self, upload, user):
        with self.catalog.transaction(True) as db:
            state = self.catalog._upload(db, upload, user)
            if state["state"] in ("complete", "completing"):
                raise Error(409, "completion intent cannot be aborted")
            db.execute("UPDATE uploads SET state='aborted' WHERE id=?", (upload,))
        return {"aborted": upload}

    def listing(
        self,
        bucket,
        user,
        prefix="",
        versions=False,
        limit=100,
        token=None,
        delimiter="",
    ):
        return list_objects(
            self.config,
            self.catalog,
            bucket,
            user,
            prefix,
            versions,
            limit,
            token,
            delimiter,
            request_fn=self._rpc,
        )

    def maintenance(self, grace=60):
        integer(grace, "grace", 0, 86400)
        resumed = 0
        pending = self.catalog.pending_publications()
        for intent in pending:
            self.publish(
                intent["bucket"],
                intent["key"],
                intent["request"],
                intent["manifest"],
                intent["size"],
                intent["sha"],
                intent["marker"],
            )
        with self.catalog.transaction() as db:
            completing = [
                dict(r)
                for r in db.execute(
                    "SELECT id,user FROM uploads WHERE state='completing'"
                )
            ]
        for row in completing:
            self.complete(row["id"], row["user"])
            resumed += 1
        with self.catalog.transaction(True) as db:
            expired = db.execute(
                "UPDATE uploads SET state='aborted' WHERE state='active' AND expires<=?",
                (self.catalog.clock(),),
            ).rowcount
        live = set()
        generations = {}
        for url in self.config["data_urls"]:
            try:
                generation = self._rpc(url + "/status", self.config["internal_token"])[
                    "gc_generation"
                ]
                if (
                    not isinstance(generation, str)
                    or len(generation) != 32
                    or any(c not in "0123456789abcdef" for c in generation)
                ):
                    raise ValueError("invalid node collection generation")
                generations[url] = generation
            except (Error, OSError, KeyError, TypeError, ValueError):
                # Unreachable nodes cannot receive a safe mark-and-sweep request.
                pass
        for url in self.config["metadata_urls"]:
            result = self._rpc(
                url + "/references", self.config["internal_token"], {"grace": grace}
            )
            if (
                not isinstance(result, dict)
                or set(result) != {"hashes"}
                or not isinstance(result["hashes"], list)
                or len(result["hashes"]) > 50000
            ):
                raise Error(502, "invalid metadata references; collection stopped")
            try:
                live.update(checksum(key) for key in result["hashes"])
            except ValueError:
                raise Error(
                    502, "invalid metadata references; collection stopped"
                ) from None
            if len(live) > 50000:
                raise Error(422, "global maintenance reference budget exceeded")
        with self.catalog.transaction() as db:
            for row in db.execute(
                "SELECT p.manifest FROM parts p JOIN uploads u ON u.id=p.upload WHERE u.state IN('active','completing')"
            ):
                live.update(c["hash"] for c in manifest_copy(json.loads(row[0])))
                if len(live) > 50000:
                    raise Error(422, "global maintenance reference budget exceeded")
        if len(live) > 50000:
            raise Error(422, "global maintenance reference budget exceeded")
        repaired, unrecoverable, copy_failures = 0, [], []
        for key in sorted(live):
            try:
                data = self.read_chunk(key)
            except Error:
                unrecoverable.append(key)
                continue
            for url in self.config["data_urls"]:
                try:
                    good = self._rpc(
                        url + "/chunks/" + key, self.config["internal_token"], raw=True
                    )
                    if sha(good) == key:
                        continue
                except Exception:
                    pass
                try:
                    receipt = self._rpc(
                        url + "/chunks/" + key,
                        self.config["internal_token"],
                        data,
                        "PUT",
                    )
                    if (
                        not isinstance(receipt, dict)
                        or receipt.get("hash") != key
                        or type(receipt.get("size")) is not int
                        or receipt["size"] != len(data)
                    ):
                        raise Error(502, "invalid repair receipt")
                    repaired += 1
                except Exception:
                    copy_failures.append({"hash": key, "url": url})
        nodes = []
        for url in self.config["data_urls"]:
            if url not in generations:
                nodes.append({"url": url, "collection_deferred": True})
                continue
            try:
                result = self._rpc(
                    url + "/collect",
                    self.config["internal_token"],
                    {
                        "live": sorted(live),
                        "grace": grace,
                        "generation": generations[url],
                    },
                )
                nodes.append({"url": url, **result})
            except Exception:
                nodes.append({"url": url, "collection_deferred": True})
        return {
            "expired_uploads": expired,
            "resumed_completions": resumed,
            "resumed_publications": len(pending),
            "live_chunks": len(live),
            "repaired_copies": repaired,
            "unrecoverable": unrecoverable,
            "copy_failures": copy_failures,
            "nodes": nodes,
        }
