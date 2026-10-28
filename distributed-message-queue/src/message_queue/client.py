import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading

from .common import dumps, integer, name, rpc
from .producer import BufferedProducer as BufferedProducer
from .producer import Producer, freeze_messages


class Client:
    def __init__(self, url, token, *, async_capacity=64, async_workers=4):
        integer(async_capacity, 1, 10000)
        integer(async_workers, 1, 32)
        self.url, self.token = url, token
        self._condition = threading.Condition()
        self._local = threading.local()
        self._closed = False
        self._active = 0
        self._executor = None
        self._async_workers = async_workers
        self._slots = threading.BoundedSemaphore(async_capacity)

    def call(self, path, **body):
        with self._condition:
            if self._closed:
                raise RuntimeError("client is closed")
            self._active += 1
            self._local.active = getattr(self._local, "active", 0) + 1
        try:
            return rpc(self.url, path, body, self.token, timeout=15)
        finally:
            with self._condition:
                self._active -= 1
                self._local.active -= 1
                self._condition.notify_all()

    def close(self):
        if getattr(self._local, "worker", False) or getattr(self._local, "active", 0):
            raise RuntimeError(
                "cannot synchronously close from a client operation or callback"
            )
        with self._condition:
            self._closed = True
            executor = self._executor
        if executor:
            executor.shutdown(wait=True)
        with self._condition:
            self._condition.wait_for(lambda: self._active == 0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def producer(self, producer):
        return Producer(self, self.call("/producer", producer=producer))

    def fire_and_forget(self, request):
        if not isinstance(request, dict):
            raise ValueError("request object required")
        owned = json.loads(dumps({**request, "acks": "leader"}))

        def send():
            # Retain this worker marker for Future callbacks, which the executor
            # invokes after send returns on the same owned thread.
            self._local.worker = True
            try:
                # Accepted work remains allowed while close drains the executor.
                rpc(self.url, "/produce", owned, self.token, timeout=15)
            finally:
                self._slots.release()

        with self._condition:
            if self._closed:
                raise RuntimeError("client is closed")
            if not self._slots.acquire(blocking=False):
                raise BufferError("async client capacity reached")
            try:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=self._async_workers,
                        thread_name_prefix="queue-client",
                    )
                future = self._executor.submit(send)
                future.add_done_callback(
                    lambda done: self._slots.release() if done.cancelled() else None
                )
                return future
            except BaseException:
                self._slots.release()
                raise

    def retry(
        self,
        txid,
        source,
        expected_offset,
        next_offset,
        record,
        retry_topic,
        dead_topic,
        attempt=0,
        max_attempts=3,
        delay_seconds=1,
    ):
        integer(attempt, 0, 10000)
        integer(max_attempts, 0, 10000)
        attempt += 1
        dead = attempt > max_attempts
        value = json.dumps(
            {
                "payload": record["value"],
                "attempt": attempt,
                "original_offset": record["offset"],
            }
        )
        return self.call(
            "/process",
            txid=txid,
            source=source,
            expected_offset=expected_offset,
            next_offset=next_offset,
            outputs=[
                {
                    "topic": dead_topic if dead else retry_topic,
                    "partition": 0,
                    "messages": [
                        {
                            "value": value,
                            "key": record.get("key"),
                            "delay_seconds": 0 if dead else delay_seconds,
                        }
                    ],
                }
            ],
        )

    def archive(self, topic, partition, path):
        """Stream a bounded retained-prefix snapshot into a new private file.

        Retention may race and abort this operation; it is not a catalog pin.
        A failed archive is removed only if its filesystem identity still matches.
        """
        name(topic)
        integer(partition, 0, 63)
        metadata = self.call("/metadata")
        state = next(
            (
                row
                for row in metadata["partitions"]
                if row["topic"] == topic and row["partition"] == partition
            ),
            None,
        )
        if state is None:
            raise ValueError("partition not found")
        offset = state["floor"]
        if state["hwm"] - offset + 1 > 100000:
            raise ValueError("archive exceeds 100000 records")
        destination = Path(path)
        count, size = 0, 0
        output = destination.open("xb")
        created = os.fstat(output.fileno())

        def write(text):
            nonlocal size
            payload = text.encode("utf-8")
            size += len(payload)
            if size > 64 * 1024 * 1024:
                raise ValueError("archive exceeds 64 MiB")
            output.write(payload)

        try:
            with output:
                write(
                    '{"topic":'
                    + dumps(topic)
                    + ',"partition":'
                    + str(partition)
                    + ',"records":['
                )
                while offset <= state["hwm"]:
                    batch = self.call(
                        "/fetch",
                        topic=topic,
                        partition=partition,
                        offset=offset,
                        limit=min(1000, state["hwm"] - offset + 1),
                    )
                    records = batch["records"]
                    if (
                        not records
                        or batch["next_offset"] != offset + len(records)
                        or len(records) > state["hwm"] - offset + 1
                    ):
                        raise ValueError(
                            "incomplete archive; retry after delayed records become due"
                        )
                    for record in records:
                        if record["offset"] != offset:
                            raise ValueError("archive offset discontinuity")
                        write(("," if count else "") + dumps(record))
                        count += 1
                        offset += 1
                write("]}")
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            try:
                current = destination.stat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    destination.unlink()
            except FileNotFoundError:
                pass
            raise
        return {"records": count}

    def replay(self, path, producer, target_topic):
        name(target_topic)
        with Path(path).open("rb") as source:
            raw = source.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("archive exceeds 64 MiB")
        body = json.loads(raw)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("records"), list)
            or len(body["records"]) > 100000
        ):
            raise ValueError("invalid archive")
        # Validate the whole file before the first side effect. Payload/key/tags
        # are replayed, while source offsets and delivery times are intentionally new.
        messages = [
            {
                "value": record["value"],
                "key": record.get("key"),
                **({"tags": record["tags"]} if "tags" in record else {}),
            }
            for record in body["records"]
        ]
        for start in range(0, len(messages), 100):
            freeze_messages(messages[start : start + 100])
        count = 0
        for start in range(0, len(messages), 100):
            batch = messages[start : start + 100]
            producer.send(
                target_topic,
                batch,
                partition=0,
            )
            count += len(batch)
        return {"replayed": count, "offsets": "new target offsets"}
