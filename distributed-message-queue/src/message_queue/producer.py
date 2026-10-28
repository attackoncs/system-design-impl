"""Partition-aware producers and an explicitly owned, bounded batching worker."""

from collections import deque
from concurrent.futures import Future
import hashlib
import math
import secrets
import threading
import time
from urllib.error import HTTPError
import zlib

from .common import dumps, integer, name
from .messages import freeze_messages


class Producer:
    def __init__(self, client, identity):
        self.client = client
        self.producer = name(identity["producer"])
        self.epoch = integer(identity["epoch"])
        self.sequences = {}
        self._pending = {}
        self._state_lock = threading.Lock()
        self._locks = tuple(threading.RLock() for _ in range(64))

    def _lock(self, key):
        return self._locks[zlib.crc32(dumps(key).encode()) % len(self._locks)]

    def _count(self, topic):
        metadata = self.client.call("/metadata")
        for row in metadata["topics"]:
            if row["name"] == topic:
                return integer(row["partitions"], 1, 64)
        raise ValueError("topic not found")

    @staticmethod
    def _partition(message, count):
        key = message.get("key")
        return (
            secrets.randbelow(count)
            if key is None
            else zlib.crc32(key.encode()) % count
        )

    def _send(self, topic, messages, partition, acks, retries):
        key = (topic, partition)
        with self._lock(key):
            fingerprint = hashlib.sha256(dumps(messages).encode()).hexdigest()
            with self._state_lock:
                sequence = self.sequences.get(key, 0)
                pending = self._pending.get(key)
            if pending and pending[0] != fingerprint:
                raise ValueError(
                    "unconfirmed sequence: resend identical messages to this partition first"
                )
            body = {
                "topic": topic,
                "partition": partition,
                "producer": self.producer,
                "epoch": self.epoch,
                "sequence": sequence,
                "messages": messages,
                "acks": acks,
            }
            with self._state_lock:
                self._pending[key] = (fingerprint, messages)
            for attempt in range(retries + 1):
                try:
                    result = self.client.call("/produce", **body)
                    if result.get("partition") != partition or len(
                        result.get("offsets", [])
                    ) != len(messages):
                        raise ValueError("invalid producer acknowledgement")
                    with self._state_lock:
                        self.sequences[key] = sequence + 1
                        self._pending.pop(key, None)
                    return result
                except HTTPError as exc:
                    status = exc.code
                    exc.close()
                    if status != 503 or attempt == retries:
                        raise
                except OSError:
                    if attempt == retries:
                        raise
                time.sleep(0.1 * (attempt + 1))

    @staticmethod
    def _options(topic, partition, acks, retries):
        name(topic)
        if partition is not None:
            integer(partition, 0, 63)
        integer(retries, 0, 10)
        if acks not in ("all", "leader"):
            raise ValueError("acks must be all or leader")

    def send(self, topic, messages, partition=None, acks="all", retries=2):
        """Send one partition batch; use send_many for messages spanning keys."""
        self._options(topic, partition, acks, retries)
        messages = freeze_messages(messages)
        if partition is None:
            count = self._count(topic)
            keyed = {
                self._partition(m, count) for m in messages if m.get("key") is not None
            }
            if len(keyed) > 1:
                raise ValueError(
                    "keys span partitions; use send_many or an explicit partition"
                )
            partition = next(iter(keyed)) if keyed else secrets.randbelow(count)
            fingerprint = hashlib.sha256(dumps(messages).encode()).hexdigest()
            with self._state_lock:
                matching = [
                    p
                    for (t, p), value in self._pending.items()
                    if t == topic and value[0] == fingerprint
                ]
            if matching:
                partition = matching[0]
        return self._send(topic, messages, partition, acks, retries)

    def retry_pending(self, topic, partition, *, acks="all", retries=2):
        """Confirm the exact in-memory batch after an ambiguous result."""
        self._options(topic, partition, acks, retries)
        with self._lock((topic, partition)):
            with self._state_lock:
                pending = self._pending.get((topic, partition))
            if pending is None:
                raise ValueError("no unconfirmed batch for this partition")
            return self._send(topic, pending[1], partition, acks, retries)

    def send_many(
        self, topic, messages, partition=None, *, batch_size=100, acks="all", retries=2
    ):
        """Group by key, retaining input order per partition; not a transaction.

        Each receipt includes input_indexes for its offsets. Earlier groups can
        succeed before a later failure; retry/recovery remains per partition.
        """
        return list(
            self._send_many(
                topic,
                messages,
                partition,
                batch_size=batch_size,
                acks=acks,
                retries=retries,
            )
        )

    def _send_many(
        self, topic, messages, partition=None, *, batch_size=100, acks="all", retries=2
    ):
        self._options(topic, partition, acks, retries)
        integer(batch_size, 1, 100)
        messages = freeze_messages(messages, maximum=1000)
        count = self._count(topic) if partition is None else None
        groups = {}
        for index, message in enumerate(messages):
            target = (
                partition if partition is not None else self._partition(message, count)
            )
            groups.setdefault(target, []).append((index, message))
        for target, group in groups.items():
            for start in range(0, len(group), batch_size):
                chunk = group[start : start + batch_size]
                receipt = self._send(
                    topic, [m for _, m in chunk], target, acks, retries
                )
                yield {**receipt, "input_indexes": [i for i, _ in chunk]}

    def buffered(self, topic, **options):
        return BufferedProducer(self, topic, **options)


class BufferedProducer:
    """One worker, bounded outstanding records, max batch size and max linger.

    Futures expose failures. flush/close drain accepted work; cancellation before
    dispatch avoids sending a record. No retry survives process exit.
    """

    def __init__(
        self,
        producer,
        topic,
        *,
        partition=None,
        batch_size=100,
        linger_seconds=0.01,
        max_pending=1000,
        acks="all",
        retries=2,
    ):
        Producer._options(topic, partition, acks, retries)
        integer(batch_size, 1, 100)
        integer(max_pending, 1, 10000)
        if (
            type(linger_seconds) not in (int, float)
            or not math.isfinite(linger_seconds)
            or not 0 <= linger_seconds <= 5
        ):
            raise ValueError("linger must be 0..5 seconds")
        self.producer, self.topic, self.partition = producer, topic, partition
        self.batch_size, self.linger = batch_size, linger_seconds
        self.max_pending, self.acks, self.retries = max_pending, acks, retries
        self._condition = threading.Condition()
        self._queue = deque()
        self._pending = 0
        self._closing = False
        self._flushing = False
        self._thread = threading.Thread(target=self._run, name="queue-producer-batch")
        self._thread.start()

    def submit(self, message):
        owned = freeze_messages([message])[0]
        future = Future()
        with self._condition:
            if self._closing:
                raise RuntimeError("buffered producer is closed")
            if self._pending >= self.max_pending:
                raise BufferError("producer pending capacity reached")
            self._queue.append((time.monotonic(), owned, future))
            self._pending += 1
            self._condition.notify_all()
        return future

    def flush(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot synchronously flush from a producer callback")
        with self._condition:
            self._flushing = True
            self._condition.notify_all()
            self._condition.wait_for(lambda: self._pending == 0)
            self._flushing = False

    def close(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError("cannot synchronously close from a producer callback")
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._thread.join()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._queue or self._closing)
                if not self._queue and self._closing:
                    return
                while (
                    not self._closing
                    and not self._flushing
                    and len(self._queue) < self.batch_size
                ):
                    remaining = self._queue[0][0] + self.linger - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)
                entries = [
                    self._queue.popleft()
                    for _ in range(min(len(self._queue), self.batch_size))
                ]
            active = [
                (message, future)
                for _, message, future in entries
                if future.set_running_or_notify_cancel()
            ]
            try:
                if active:
                    receipts = self.producer._send_many(
                        self.topic,
                        [m for m, _ in active],
                        self.partition,
                        batch_size=self.batch_size,
                        acks=self.acks,
                        retries=self.retries,
                    )
                    for receipt in receipts:
                        for index, offset in zip(
                            receipt["input_indexes"], receipt["offsets"]
                        ):
                            active[index][1].set_result(
                                {
                                    "topic": self.topic,
                                    "partition": receipt["partition"],
                                    "offset": offset,
                                    "committed": receipt["committed"],
                                    "replicas": list(receipt.get("replicas", [])),
                                }
                            )
            except Exception as exc:
                for _, future in active:
                    if not future.done():
                        future.set_exception(exc)
            finally:
                with self._condition:
                    self._pending -= len(entries)
                    self._condition.notify_all()
