from pathlib import Path
import threading

from .common import integer, name
from .log import PartitionLog


class Broker:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs = {}
        self.lock = threading.RLock()

    def dispatch(self, path, body):
        if path == "/health":
            return {"role": "broker"}
        topic = name(body["topic"])
        partition = integer(body["partition"], 0, 63)
        key = (topic, partition)
        with self.lock:
            if key not in self.logs:
                self.logs[key] = PartitionLog(
                    self.root / topic / str(partition),
                    body.get("segment_bytes", 1024 * 1024),
                )
            log = self.logs[key]
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
                log.align(body["epoch"], body["committed"], body["committed_hash"])
                if body["base"] > log.base:
                    log.retain(body["base"], body["base_hash"])
                log.append(body["records"])
                return {"next": log.next, "hash": log.hash_at(log.next - 1)}
            if path == "/install":
                log.install(
                    body["records"], body["base"], body["base_hash"], body["epoch"]
                )
                return {"next": log.next, "hash": log.hash_at(log.next - 1)}
            if path == "/retain":
                log.retain(body["base"], body["base_hash"])
                return {"base": log.base}
            raise ValueError("unknown broker operation")
