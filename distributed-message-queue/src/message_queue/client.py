import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
import zlib

from .common import rpc


class Client:
    def __init__(self, url, token):
        self.url, self.token = url, token

    def call(self, path, **body):
        return rpc(self.url, path, body, self.token, timeout=15)

    def producer(self, producer):
        return Producer(self, self.call("/producer", producer=producer))

    def fire_and_forget(self, request):
        def send():
            try:
                self.call("/produce", **{**request, "acks": "leader"})
            except Exception:
                pass

        threading.Thread(target=send, daemon=True).start()

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
        metadata = self.call("/metadata")
        state = next(
            row
            for row in metadata["partitions"]
            if row["topic"] == topic and row["partition"] == partition
        )
        offset = state["floor"]
        records = []
        while offset <= state["hwm"]:
            batch = self.call(
                "/fetch",
                topic=topic,
                partition=partition,
                offset=offset,
                limit=min(1000, state["hwm"] - offset + 1),
            )
            if not batch["records"]:
                raise ValueError(
                    "delayed records prevent a complete archive; retry after due time"
                )
            records.extend(batch["records"])
            offset = batch["next_offset"]
        # Exclusive creation prevents overwriting an existing user archive.
        with Path(path).open("x", encoding="utf-8") as output:
            json.dump(
                {"topic": topic, "partition": partition, "records": records}, output
            )
        return {"records": len(records)}

    def replay(self, path, producer, target_topic):
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        count = 0
        for record in body["records"]:
            producer.send(
                target_topic,
                [{"value": record["value"], "key": record.get("key")}],
                partition=0,
            )
            count += 1
        return {"replayed": count, "offsets": "new target offsets"}


class Producer:
    def __init__(self, client, identity):
        self.client, self.producer, self.epoch = (
            client,
            identity["producer"],
            identity["epoch"],
        )
        self.sequences = {}
        self.lock = threading.Lock()

    def send(self, topic, messages, partition=None, acks="all", retries=2):
        with self.lock:
            if partition is None:
                metadata = self.client.call("/metadata")
                count = next(
                    row["partitions"]
                    for row in metadata["topics"]
                    if row["name"] == topic
                )
                partition = (
                    zlib.crc32(str(messages[0].get("key", self.producer)).encode())
                    % count
                )
            key = (topic, partition)
            sequence = self.sequences.get(key, 0)
            body = {
                "topic": topic,
                "partition": partition,
                "producer": self.producer,
                "epoch": self.epoch,
                "sequence": sequence,
                "messages": messages,
                "acks": acks,
            }
            for attempt in range(retries + 1):
                try:
                    result = self.client.call("/produce", **body)
                    self.sequences[key] = sequence + 1
                    return result
                except HTTPError as exc:
                    if exc.code != 503 or attempt == retries:
                        raise
                except OSError:
                    if attempt == retries:
                        raise
                time.sleep(0.1 * (attempt + 1))
