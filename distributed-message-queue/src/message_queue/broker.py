from concurrent.futures import Future
from pathlib import Path
import threading

from .common import Error, integer, name
from .log import PartitionLog


class Broker:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs = {}
        self._loading = {}
        self.lock = threading.RLock()

    def _log(self, key, segment_bytes):
        with self.lock:
            if key in self.logs:
                return self.logs[key]
            pending = self._loading.get(key)
            owner = pending is None
            if owner:
                pending = Future()
                self._loading[key] = pending
        if not owner:
            return pending.result()
        try:
            topic, partition = key
            log = PartitionLog(self.root / topic / str(partition), segment_bytes)
        except BaseException as exc:
            with self.lock:
                del self._loading[key]
            pending.set_exception(exc)
            raise
        with self.lock:
            self.logs[key] = log
            del self._loading[key]
        pending.set_result(log)
        return log

    def dispatch(self, path, body):
        if path == "/health":
            return {"role": "broker"}
        if path not in {"/status", "/read", "/sync", "/install", "/retain"}:
            raise Error(404, "unknown broker operation")
        topic = name(body["topic"])
        partition = integer(body["partition"], 0, 63)
        key = (topic, partition)
        segment_bytes = integer(
            body.get("segment_bytes", 1024 * 1024), 256, 16 * 1024 * 1024
        )
        log = self._log(key, segment_bytes)
        # Log-owned locks serialize one partition without holding a broker-wide
        # lock across disk I/O on unrelated partitions.
        with log.lock:
            if path == "/status":
                # Small reference favors integrity over throughput: verify before eligibility.
                log.scan()
                return {
                    "base": log.base,
                    "next": log.next,
                    "epoch": log.manifest["epoch"],
                    "hash": log.hash_at(body["committed"]),
                }
            if path == "/read":
                return {"records": log.read(body["offset"], body.get("limit", 1000))}
            if path == "/sync":
                log.synchronize(
                    body["epoch"],
                    body["committed"],
                    body["committed_hash"],
                    body["base"],
                    body["base_hash"],
                    body["records"],
                )
                return {"next": log.next, "hash": log.hash_at(log.next - 1)}
            if path == "/install":
                log.install(
                    body["records"],
                    body["base"],
                    body["base_hash"],
                    body["epoch"],
                    body.get("committed"),
                    body.get("committed_hash"),
                )
                return {"next": log.next, "hash": log.hash_at(log.next - 1)}
            if path == "/retain":
                log.retain(body["base"], body["base_hash"])
                return {"base": log.base}
