"""Bounded prefix retention and durable retry of remote physical cleanup.

The metadata floor commits before contacting brokers. A per-node journal survives
coordinator restarts; no network operation runs inside the SQLite writer.
"""

import json
import logging

from .common import Error
from .coordination_locks import partition_key


LOGGER = logging.getLogger("message_queue.coordinator")


class Retention:
    def retain(self, topic):
        with self.transaction(write=False) as db:
            options = self.topic(db, topic)
        plans = []
        for partition in range(options["partitions"]):
            with self.operations.hold(partition_key(topic, partition)):
                plan = self._retain_partition(topic, partition, options)
                if plan:
                    plans.append(plan)
                self._retry_retention(topic, partition)
        return {"retention": plans}

    def _retain_partition(self, topic, partition, options):
        with self.transaction() as db:
            part = self.part(db, topic, partition)
            now = self._now()
            size = db.execute(
                "SELECT COALESCE(SUM(size),0) FROM records WHERE topic=? AND partition=?",
                (topic, partition),
            ).fetchone()[0]
            records = db.execute(
                "SELECT * FROM records WHERE topic=? AND partition=? ORDER BY offset LIMIT 4096",
                (topic, partition),
            )
            floor, base_hash = part["floor"], part["base_hash"]
            for record in records:
                if (
                    record["timestamp"] > now - options["retention"]
                    and size <= options["max_bytes"]
                ):
                    break
                floor, base_hash = record["offset"] + 1, record["hash"]
                size -= record["size"]
            if floor <= part["floor"]:
                return None
            db.execute(
                "UPDATE parts SET floor=?,base_hash=? WHERE topic=? AND partition=?",
                (floor, base_hash, topic, partition),
            )
            db.execute(
                "DELETE FROM records WHERE topic=? AND partition=? AND offset<?",
                (topic, partition, floor),
            )
            db.execute(
                "DELETE FROM receipts WHERE topic=? AND partition=? AND last_offset<?",
                (topic, partition, floor),
            )
            db.executemany(
                "INSERT INTO retention_pending VALUES(?,?,?,?,?) ON CONFLICT(topic,partition,node) DO UPDATE SET base=excluded.base,base_hash=excluded.base_hash",
                [(topic, partition, node, floor, base_hash) for node in self.brokers],
            )
            return {
                "topic": topic,
                "partition": partition,
                "base": floor,
                "base_hash": base_hash,
            }

    def _retry_retention(self, topic, partition):
        with self.transaction(write=False) as db:
            pending = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM retention_pending WHERE topic=? AND partition=?",
                    (topic, partition),
                )
            ]
        for item in pending:
            plan = {
                key: item[key] for key in ("topic", "partition", "base", "base_hash")
            }
            try:
                result = self.call(item["node"], "/retain", plan)
                if type(result.get("base")) is not int or result["base"] < item["base"]:
                    raise ValueError("retention acknowledgement mismatch")
                with self.transaction() as db:
                    db.execute(
                        "DELETE FROM retention_pending WHERE topic=? AND partition=? AND node=? AND base=? AND base_hash=?",
                        (
                            topic,
                            partition,
                            item["node"],
                            item["base"],
                            item["base_hash"],
                        ),
                    )
            except (Error, OSError, ValueError, TypeError, KeyError):
                self.last_error = "replica retention pending"
                LOGGER.warning(
                    "replica retention pending; a later maintenance pass will retry"
                )

    def maintenance(self):
        failed = False
        with self.transaction(write=False) as db:
            pending = [dict(row) for row in db.execute("SELECT * FROM pending")]
            topics = [row[0] for row in db.execute("SELECT name FROM topics")]
        for row in pending:
            if not json.loads(row["body"]).get("transaction"):
                try:
                    with self.operations.hold(
                        partition_key(row["topic"], row["partition"])
                    ):
                        self._flush(row["topic"], row["partition"])
                except (Error, OSError, ValueError):
                    failed = True
                    LOGGER.warning(
                        "provisional queue append pending; a later maintenance pass will retry"
                    )
        for topic in topics:
            try:
                self.retain(topic)
            except (Error, OSError, ValueError):
                failed = True
                LOGGER.warning(
                    "queue retention failed; a later maintenance pass will retry"
                )
        with self.transaction(write=False) as db:
            cleanup_pending = bool(
                db.execute("SELECT 1 FROM retention_pending LIMIT 1").fetchone()
            )
        self.last_error = (
            "maintenance retry pending" if failed or cleanup_pending else None
        )
