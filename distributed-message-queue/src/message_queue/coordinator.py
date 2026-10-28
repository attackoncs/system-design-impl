from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import zlib

from .common import Error, ZERO, dumps, envelope, integer, name, rpc
from .coordination_locks import OperationLocks, partition_key
from .coordinator_groups import ConsumerGroups
from .coordinator_replication import Replication
from .coordinator_retention import Retention
from .coordinator_schema import initialize
from .coordinator_validation import finite_number, tags_filter
from .messages import freeze_messages


class Coordinator(ConsumerGroups, Replication, Retention):
    """Single metadata owner; publication is atomic, partition protocols concurrent.

    Ordered fixed stripes bound lock memory; collisions may serialize unrelated
    partitions. Sharing this database between concurrent coordinator processes is
    not a substitute for a consensus-backed metadata service.
    """

    def __init__(self, root, brokers, cluster_token, lease_seconds=10, clock=time.time):
        if not isinstance(brokers, dict) or not 1 <= len(brokers) <= 9:
            raise ValueError("1..9 brokers required")
        for node, url in brokers.items():
            name(node)
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ValueError("broker URL required")
        if not isinstance(cluster_token, str) or len(cluster_token) < 8:
            raise ValueError("private cluster credential required")
        finite_number(lease_seconds, 0.01, 86400, "invalid consumer lease")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "metadata.sqlite"
        self.brokers = dict(brokers)
        self.token, self.lease_seconds, self.clock = cluster_token, lease_seconds, clock
        self.operations = OperationLocks()
        self.changed = threading.Condition()
        self.last_error = None
        with closing(self.connect()) as db:
            initialize(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def transaction(self, write=True):
        with closing(self.connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _now(self):
        return finite_number(self.clock(), 0, 2**53 - 1, "invalid clock")

    def _notify(self):
        with self.changed:
            self.changed.notify_all()

    def _poll(self, page, wait_seconds):
        """Wait between short operations, never while holding partition/SQL locks.

        The wait budget does not interrupt an already accepted broker RPC. The
        100ms fallback observes due-time changes and other process notifications.
        """
        finite_number(wait_seconds, 0, 5, "wait_seconds must be 0..5")
        deadline = time.monotonic() + wait_seconds
        while True:
            result, scanned = page()
            if result["records"] or scanned or not wait_seconds:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            with self.changed:
                self.changed.wait(min(0.1, remaining))

    def topic(self, db, topic):
        name(topic)
        row = db.execute("SELECT * FROM topics WHERE name=?", (topic,)).fetchone()
        if not row:
            raise Error(404, "topic not found")
        return dict(row)

    def part(self, db, topic, partition):
        self.topic(db, topic)
        integer(partition, 0, 63)
        row = db.execute(
            "SELECT * FROM parts WHERE topic=? AND partition=?", (topic, partition)
        ).fetchone()
        if not row:
            raise Error(404, "partition not found")
        return dict(row)

    def expected_hash(self, db, part):
        if part["hwm"] < part["floor"]:
            return part["base_hash"]
        return db.execute(
            "SELECT hash FROM records WHERE topic=? AND partition=? AND offset=?",
            (part["topic"], part["partition"], part["hwm"]),
        ).fetchone()[0]

    def create(
        self,
        topic,
        partitions=1,
        retention_seconds=14 * 86400,
        max_bytes=8 * 1024 * 1024,
        segment_bytes=1024 * 1024,
    ):
        name(topic)
        integer(partitions, 1, 64)
        integer(max_bytes, 1024, 16 * 1024 * 1024)
        integer(segment_bytes, 256, 16 * 1024 * 1024)
        finite_number(retention_seconds, 0.001, 365 * 86400, "invalid retention")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM topics WHERE name=?", (topic,)).fetchone():
                raise Error(409, "topic already exists")
            db.execute(
                "INSERT INTO topics VALUES(?,?,?,?,?)",
                (topic, partitions, retention_seconds, max_bytes, segment_bytes),
            )
            nodes = sorted(self.brokers)
            db.executemany(
                "INSERT INTO parts VALUES(?,?,?,0,-1,0,?)",
                [(topic, p, nodes[p % len(nodes)], ZERO) for p in range(partitions)],
            )
        return {"topic": topic, "partitions": partitions}

    def producer_open(self, producer):
        name(producer)
        with self.operations.hold(("producer", producer)):
            with self.transaction() as db:
                row = db.execute(
                    "SELECT epoch FROM producers WHERE id=?", (producer,)
                ).fetchone()
                epoch = row[0] + 1 if row else 0
                db.execute(
                    "INSERT INTO producers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch",
                    (producer, epoch),
                )
                return {"producer": producer, "epoch": epoch}

    def call(self, node, path, body):
        return rpc(self.brokers[node], path, body, self.token, timeout=0.7)

    def _finalize(self, db, pending, synced):
        last = pending["values"][-1]["record"]["offset"]
        for value in pending["values"]:
            record = value["record"]
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (
                    pending["topic"],
                    pending["partition"],
                    record["offset"],
                    value["hash"],
                    record["timestamp"],
                    len(dumps(value).encode()),
                ),
            )
        db.execute(
            "UPDATE parts SET hwm=? WHERE topic=? AND partition=?",
            (last, pending["topic"], pending["partition"]),
        )
        response = {
            "topic": pending["topic"],
            "partition": pending["partition"],
            "offsets": [v["record"]["offset"] for v in pending["values"]],
            "committed": True,
            "replicas": synced,
        }
        if not pending.get("transaction"):
            args = (
                pending["producer"],
                pending["epoch"],
                pending["topic"],
                pending["partition"],
            )
            db.execute(
                "INSERT INTO sequences VALUES(?,?,?,?,?) ON CONFLICT(producer,epoch,topic,partition) DO UPDATE SET last=excluded.last",
                (*args, pending["sequence"]),
            )
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?)",
                (
                    *args,
                    pending["sequence"],
                    pending["fingerprint"],
                    dumps(response),
                    last,
                ),
            )
        db.execute(
            "DELETE FROM pending WHERE topic=? AND partition=?",
            (pending["topic"], pending["partition"]),
        )
        return response

    def _flush(self, topic, partition):
        with self.transaction(write=False) as db:
            row = db.execute(
                "SELECT body FROM pending WHERE topic=? AND partition=?",
                (topic, partition),
            ).fetchone()
        if not row:
            return None
        pending = json.loads(row[0])
        if pending.get("transaction"):
            raise Error(409, "partition reserved by unfinished transaction")
        synced = self._replicate(pending)
        with self.transaction() as db:
            response = self._finalize(db, pending, synced)
        self._notify()
        return response

    def _values(self, part, messages, producer, epoch, sequence):
        """Build a hash-linked batch from already validated, owned messages."""
        with self.transaction(write=False) as db:
            previous = self.expected_hash(db, part)
        now = self._now()
        values = []
        for i, message in enumerate(messages):
            record = {
                "offset": part["hwm"] + 1 + i,
                "timestamp": now,
                "producer": producer,
                "producer_epoch": epoch,
                "sequence": sequence,
                "key": message.get("key"),
                "value": message["value"],
                "not_before": message.get(
                    "deliver_at", now + message.get("delay_seconds", 0)
                ),
            }
            if message.get("tags"):
                record["tags"] = list(message["tags"])
            value = envelope(record, previous)
            values.append(value)
            previous = value["hash"]
        return values

    def produce(
        self, topic, producer, epoch, sequence, messages, partition=None, acks="all"
    ):
        name(producer)
        integer(epoch)
        integer(sequence)
        if acks not in ("all", "leader"):
            raise ValueError("acks must be all or leader")
        # Freeze caller-owned nested dictionaries before waiting on another writer.
        messages = freeze_messages(messages)
        with self.transaction(write=False) as db:
            options = self.topic(db, topic)
            if partition is None:
                keyed_partitions = {
                    zlib.crc32(message["key"].encode()) % options["partitions"]
                    for message in messages
                    if message.get("key") is not None
                }
                if len(keyed_partitions) > 1:
                    raise ValueError("implicit batch spans multiple key partitions")
                # The SDK chooses random partitions for keyless sends explicitly.
                # Direct RPC retries need a stable fallback for the same sequence.
                partition = (
                    next(iter(keyed_partitions))
                    if keyed_partitions
                    else zlib.crc32(producer.encode()) % options["partitions"]
                )
        integer(partition, 0, 63)
        with self.operations.hold(
            partition_key(topic, partition), ("producer", producer)
        ):
            return self._produce(
                topic, producer, epoch, sequence, messages, partition, acks
            )

    def _produce(self, topic, producer, epoch, sequence, messages, partition, acks):
        with self.transaction(write=False) as db:
            part = self.part(db, topic, partition)
            current = db.execute(
                "SELECT epoch FROM producers WHERE id=?", (producer,)
            ).fetchone()
            if not current or current[0] != epoch:
                raise Error(409, "producer epoch fenced")
            fingerprint = hashlib.sha256(dumps(messages).encode()).hexdigest()
            receipt = db.execute(
                "SELECT hash,response FROM receipts WHERE producer=? AND epoch=? AND topic=? AND partition=? AND sequence=?",
                (producer, epoch, topic, partition, sequence),
            ).fetchone()
            if receipt:
                if receipt[0] != fingerprint:
                    raise Error(409, "producer sequence payload conflict")
                return json.loads(receipt[1])
            pending_row = db.execute(
                "SELECT body FROM pending WHERE topic=? AND partition=?",
                (topic, partition),
            ).fetchone()
        if pending_row:
            existing = json.loads(pending_row[0])
            if (
                existing.get("producer") == producer
                and existing.get("epoch") == epoch
                and existing.get("sequence") == sequence
            ):
                if existing["fingerprint"] != fingerprint:
                    raise Error(409, "pending producer sequence payload conflict")
                if acks == "leader":
                    self._replicate(existing, leader_only=True)
                    return {
                        "topic": topic,
                        "partition": partition,
                        "offsets": [v["record"]["offset"] for v in existing["values"]],
                        "committed": False,
                    }
                return self._flush(topic, partition)
            self._flush(topic, partition)
        with self.transaction(write=False) as db:
            part = self.part(db, topic, partition)
            last = db.execute(
                "SELECT last FROM sequences WHERE producer=? AND epoch=? AND topic=? AND partition=?",
                (producer, epoch, topic, partition),
            ).fetchone()
            expected = last[0] + 1 if last else 0
            if sequence < expected:
                raise Error(
                    410, "producer receipt expired; old sequence cannot be reused"
                )
            if sequence != expected:
                raise Error(409, "producer sequence gap")
        values = self._values(part, messages, producer, epoch, sequence)
        pending = {
            "topic": topic,
            "partition": partition,
            "producer": producer,
            "epoch": epoch,
            "sequence": sequence,
            "fingerprint": fingerprint,
            "values": values,
        }
        with self.transaction() as db:
            db.execute(
                "INSERT INTO pending VALUES(?,?,?)", (topic, partition, dumps(pending))
            )
        if acks == "leader":
            self._replicate(pending, leader_only=True)
            return {
                "topic": topic,
                "partition": partition,
                "offsets": [v["record"]["offset"] for v in values],
                "committed": False,
            }
        return self._flush(topic, partition)

    def fetch(self, topic, partition, offset=0, limit=100, tags=None, wait_seconds=0):
        name(topic)
        integer(partition, 0, 63)
        filters = tags_filter(tags)

        def page():
            with self.operations.hold(partition_key(topic, partition)):
                result = self._fetch(topic, partition, offset, limit, filters)
                return result, result["next_offset"] != offset

        return self._poll(page, wait_seconds)

    def _fetch(self, topic, partition, offset=0, limit=100, filters=None):
        integer(offset)
        integer(limit, 1, 1000)
        with self.transaction(write=False) as db:
            part = self.part(db, topic, partition)
        if offset < part["floor"]:
            raise Error(410, "offset expired; explicitly reset to earliest")
        if offset > part["hwm"] + 1:
            raise Error(416, "offset beyond committed high water")
        if offset == part["hwm"] + 1:
            return {
                "records": [],
                "next_offset": offset,
                "high_water": part["hwm"],
                "floor": part["floor"],
            }
        part, leader, _ = self._leader(part)
        values = self._read_replica(
            part, leader, offset, min(limit, part["hwm"] - offset + 1)
        )
        visible = []
        next_offset = offset
        now = self._now()
        for value in values:
            if value["record"]["not_before"] > now:
                break
            next_offset += 1
            if filters is None or filters.intersection(value["record"].get("tags", [])):
                visible.append(value["record"])
        return {
            "records": visible,
            "next_offset": next_offset,
            "high_water": part["hwm"],
            "floor": part["floor"],
            "leader": leader,
            "epoch": part["epoch"],
        }

    def process(self, txid, source, expected_offset, next_offset, outputs):
        name(txid)
        integer(expected_offset)
        integer(next_offset)
        if not isinstance(source, dict) or set(source) != {
            "topic",
            "group",
            "member",
            "session",
            "generation",
            "partition",
        }:
            raise ValueError("complete consumer source required")
        source = json.loads(dumps(source))
        for field in ("topic", "group", "member", "session"):
            name(source[field])
        integer(source["generation"])
        integer(source["partition"], 0, 63)
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 10:
            raise ValueError("transaction requires 1..10 output batches")
        captured = list(outputs)
        outputs = []
        destinations = []
        for output in captured:
            if (
                not isinstance(output, dict)
                or not {"topic", "messages"} <= set(output)
                or set(output) - {"topic", "messages", "partition"}
            ):
                raise ValueError("invalid transaction output")
            destinations.append(
                partition_key(
                    name(output["topic"]), integer(output.get("partition", 0), 0, 63)
                )
            )
            outputs.append({**output, "messages": freeze_messages(output["messages"])})
        if len(set(destinations)) != len(destinations):
            raise ValueError("combine outputs for each destination partition")
        keys = [
            ("transaction", txid),
            partition_key(source["topic"], source["partition"]),
            *destinations,
        ]
        with self.operations.hold(*keys):
            return self._process(txid, source, expected_offset, next_offset, outputs)

    def _process(self, txid, source, expected_offset, next_offset, outputs):
        fingerprint = hashlib.sha256(
            dumps([source, expected_offset, next_offset, outputs]).encode()
        ).hexdigest()
        with self.transaction() as db:
            replay = db.execute(
                "SELECT hash,response FROM transactions WHERE id=?", (txid,)
            ).fetchone()
            if replay:
                if replay[0] != fingerprint:
                    raise Error(409, "transaction ID payload conflict")
                return json.loads(replay[1])
            self._validate_commit(
                db, **source, next_offset=next_offset, expected=expected_offset
            )
            if next_offset <= expected_offset:
                raise ValueError("transaction must advance its input cursor")
        pending_batches = []
        partitions = set()
        try:
            for output in outputs:
                topic, partition = output["topic"], output.get("partition", 0)
                if (topic, partition) in partitions:
                    raise ValueError("combine outputs for each destination partition")
                partitions.add((topic, partition))
                self._flush(topic, partition)
                with self.transaction() as db:
                    part = self.part(db, topic, partition)
                values = self._values(part, output["messages"], "tx_" + txid, 0, 0)
                pending = {
                    "topic": topic,
                    "partition": partition,
                    "values": values,
                    "transaction": txid,
                }
                with self.transaction() as db:
                    db.execute(
                        "INSERT INTO pending VALUES(?,?,?)",
                        (topic, partition, dumps(pending)),
                    )
                pending_batches.append(pending)
            synced = [self._replicate(pending) for pending in pending_batches]
            with self.transaction() as db:
                self._validate_commit(
                    db, **source, next_offset=next_offset, expected=expected_offset
                )
                produced = [
                    self._finalize(db, pending, nodes)
                    for pending, nodes in zip(pending_batches, synced)
                ]
                db.execute(
                    "INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",
                    (
                        source["topic"],
                        source["group"],
                        source["partition"],
                        next_offset,
                    ),
                )
                response = {
                    "txid": txid,
                    "outputs": produced,
                    "next_offset": next_offset,
                    "boundary": "queue output visibility and input cursor only",
                }
                db.execute(
                    "INSERT INTO transactions VALUES(?,?,?)",
                    (txid, fingerprint, dumps(response)),
                )
            self._notify()
            return response
        except BaseException:
            with self.transaction() as db:
                for pending in pending_batches:
                    db.execute(
                        "DELETE FROM pending WHERE topic=? AND partition=? AND json_extract(body,'$.transaction')=?",
                        (pending["topic"], pending["partition"], txid),
                    )
            raise

    def metadata(self):
        with self.transaction(write=False) as db:
            return {
                "role": "coordinator",
                "metadata_ha": False,
                "coordinator_maintenance_error": self.last_error,
                "brokers": sorted(self.brokers),
                "topics": [dict(r) for r in db.execute("SELECT * FROM topics")],
                "partitions": [
                    dict(r)
                    for r in db.execute("SELECT * FROM parts ORDER BY topic,partition")
                ],
            }

    def dispatch(self, path, body):
        methods = {
            "/topic": self.create,
            "/expand": self.expand,
            "/producer": self.producer_open,
            "/produce": self.produce,
            "/fetch": self.fetch,
            "/join": self.join,
            "/leave": self.leave,
            "/heartbeat": self.heartbeat,
            "/group-fetch": self.group_fetch,
            "/commit": self.commit,
            "/reset": self.reset,
            "/process": self.process,
            "/retain": self.retain,
        }
        if not isinstance(body, dict):
            raise ValueError("request object required")
        if path in ("/health", "/metadata"):
            if body:
                raise ValueError("metadata request must be empty")
            return self.metadata()
        if path not in methods:
            raise Error(404, "unknown coordinator endpoint")
        return methods[path](**body)
