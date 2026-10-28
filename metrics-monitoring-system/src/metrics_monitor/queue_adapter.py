"""Chapter 20 wire adapter with per-partition producer and fenced group state."""

import json
import threading
from urllib.error import HTTPError
import uuid

from .collector import integer
from .common import Error, digest, http_json, identifier


class QueueAdapter:
    def __init__(
        self, url, token, topic="metrics", *, partitions=1, group="metrics-storage"
    ):
        integer(partitions, 1, 64)
        self.url, self.token, self.topic = url.rstrip("/"), token, identifier(topic)
        self.partitions, self.group = partitions, identifier(group)
        self._ensure_lock = threading.Lock()
        self._ready = False
        self._locks = tuple(threading.Lock() for _ in range(64))
        self._producers, self._sequences, self._pending = {}, {}, {}
        self._consumer_lock = threading.Lock()
        self.member = None
        self.member_id = "storage_" + uuid.uuid4().hex
        self._cursor = 0

    def call(self, path, **body):
        return http_json(self.url + path, "POST", body, self.token, timeout=15)

    def ensure(self):
        with self._ensure_lock:
            if self._ready:
                return
            try:
                self.call("/topic", topic=self.topic, partitions=self.partitions)
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if status != 409:
                    raise
            metadata = self.call("/metadata")
            topic = next(
                (row for row in metadata["topics"] if row["name"] == self.topic), None
            )
            if not topic or topic["partitions"] < self.partitions:
                raise Error(409, "queue topic needs explicit partition expansion")
            self._ready = True

    def publish(self, payload):
        if not isinstance(payload, str) or len(payload.encode()) > 12000:
            raise ValueError("queue payload exceeds collector limit")
        body = json.loads(payload)
        partition = integer(body.get("partition", 0), 0, 63)
        # Durable pending chunks keep their original target after config changes.
        fingerprint = digest(payload)
        with self._locks[partition]:
            self.ensure()
            pending = self._pending.get(partition)
            if pending is not None and pending != fingerprint:
                raise Error(
                    409, "unconfirmed queue sequence requires the original payload"
                )
            if partition not in self._producers:
                self._producers[partition] = self.call(
                    "/producer", producer="metrics_" + uuid.uuid4().hex
                )
                self._sequences[partition] = 0
            self._pending[partition] = fingerprint
            result = self.call(
                "/produce",
                topic=self.topic,
                partition=partition,
                **self._producers[partition],
                sequence=self._sequences[partition],
                messages=[{"value": payload}],
                acks="all",
            )
            if (
                result.get("committed") is not True
                or result.get("partition") != partition
                or len(result.get("offsets", [])) != 1
            ):
                raise RuntimeError("queue did not confirm the selected partition")
            self._sequences[partition] += 1
            self._pending.pop(partition, None)
            return result

    def fetch(self):
        with self._consumer_lock:
            self.ensure()
            try:
                if self.member is None:
                    self.member = self.call(
                        "/join",
                        topic=self.topic,
                        group=self.group,
                        member=self.member_id,
                    )
                else:
                    self.member = self.call(
                        "/heartbeat",
                        topic=self.topic,
                        group=self.group,
                        member=self.member_id,
                        session=self.member["session"],
                    )
            except HTTPError as exc:
                if exc.code == 409:
                    self.member = None
                raise
            assignments = self.member["assignments"]
            if not assignments:
                return {}, {"records": []}
            partition = assignments[self._cursor % len(assignments)]
            self._cursor += 1
            source = {
                "topic": self.topic,
                "group": self.group,
                "member": self.member_id,
                "session": self.member["session"],
                "generation": self.member["generation"],
                "partition": partition,
            }
            return source, self.call("/group-fetch", **source, limit=10)
