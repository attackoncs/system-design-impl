"""Leader eligibility, bounded catch-up, and replica response validation.

Called while the owning coordinator holds the partition operation lock. Network
I/O occurs outside metadata transactions; publication belongs to the coordinator.
"""

from .common import Error, dumps, envelope
from .log import HEADER, MAX_BATCH_BYTES, MAX_BATCH_RECORDS


class Replication:
    def _leader(self, part):
        with self.transaction(write=False) as db:
            part = self.part(db, part["topic"], part["partition"])
            expected = self.expected_hash(db, part)
            options = self.topic(db, part["topic"])
        common = {
            "topic": part["topic"],
            "partition": part["partition"],
            "committed": part["hwm"],
            "segment_bytes": options["segment_bytes"],
        }
        candidates = {}
        for node in [
            part["leader"],
            *[n for n in sorted(self.brokers) if n != part["leader"]],
        ]:
            try:
                state = self.call(node, "/status", common)
                if self._eligible(state, part, expected):
                    candidates[node] = state
                    if node == part["leader"]:
                        return part, node, expected
            except (Error, OSError, ValueError, KeyError, TypeError):
                pass
        if not candidates:
            raise Error(503, "no replica contains committed prefix")
        leader = sorted(candidates)[0]
        with self.transaction() as db:
            db.execute(
                "UPDATE parts SET leader=?,epoch=epoch+1 WHERE topic=? AND partition=?",
                (leader, part["topic"], part["partition"]),
            )
            part = self.part(db, part["topic"], part["partition"])
        return part, leader, expected

    @staticmethod
    def _eligible(state, part, expected):
        return (
            isinstance(state, dict)
            and state.get("hash") == expected
            and type(state.get("base")) is int
            and type(state.get("next")) is int
            and 0 <= state["base"] <= part["floor"]
            and state["next"] >= part["hwm"] + 1
        )

    def _read_replica(self, part, leader, offset, limit):
        """Verify the broker page against metadata before returning any records."""
        values = self.call(
            leader,
            "/read",
            {
                "topic": part["topic"],
                "partition": part["partition"],
                "offset": offset,
                "limit": limit,
            },
        )["records"]
        if not isinstance(values, list) or not 1 <= len(values) <= limit:
            raise Error(503, "replica omitted committed records")
        count = len(values)
        with self.transaction(write=False) as db:
            expected = [
                dict(row)
                for row in db.execute(
                    "SELECT offset,hash FROM records WHERE topic=? AND partition=? AND offset>=? AND offset<? ORDER BY offset",
                    (part["topic"], part["partition"], offset, offset + count),
                )
            ]
        if len(expected) != count:
            raise Error(503, "committed metadata range unavailable")
        try:
            for value, metadata in zip(values, expected):
                record = value["record"]
                if (
                    type(record["offset"]) is not int
                    or record["offset"] != metadata["offset"]
                    or value["hash"] != metadata["hash"]
                    or envelope(record, value["previous"]) != value
                ):
                    raise ValueError("committed record mismatch")
        except (ValueError, KeyError, TypeError) as exc:
            raise Error(503, "replica returned invalid committed records") from exc
        return values

    def _retained(self, part, leader, pending=()):
        values = []
        offset = part["floor"]
        total_bytes = sum(HEADER.size + len(dumps(value).encode()) for value in pending)
        total_records = len(pending)
        while offset <= part["hwm"]:
            batch = self._read_replica(
                part, leader, offset, min(500, part["hwm"] - offset + 1)
            )
            total_bytes += sum(
                HEADER.size + len(dumps(value).encode()) for value in batch
            )
            total_records += len(batch)
            if total_bytes > MAX_BATCH_BYTES or total_records > MAX_BATCH_RECORDS:
                raise Error(
                    503, "catchup exceeds reference snapshot budget; apply retention"
                )
            values.extend(batch)
            offset += len(batch)
        return values

    def _replicate(self, pending, leader_only=False):
        part, leader, expected = self._leader(pending)
        with self.transaction(write=False) as db:
            options = self.topic(db, part["topic"])
        common = {
            "topic": part["topic"],
            "partition": part["partition"],
            "epoch": part["epoch"],
            "committed": part["hwm"],
            "committed_hash": expected,
            "base": part["floor"],
            "base_hash": part["base_hash"],
            "segment_bytes": options["segment_bytes"],
            "records": pending["values"],
        }
        synced = []
        snapshot = None
        snapshot_failed = False
        for node in [leader, *[n for n in sorted(self.brokers) if n != leader]]:
            try:
                state = self.call(node, "/status", common)
                if self._eligible(state, part, expected):
                    result = self.call(node, "/sync", common)
                else:
                    if snapshot is None:
                        if snapshot_failed:
                            continue
                        try:
                            snapshot = self._retained(part, leader, pending["values"])
                        except (Error, OSError, ValueError, KeyError, TypeError):
                            snapshot_failed = True
                            continue
                    result = self.call(
                        node,
                        "/install",
                        {**common, "records": snapshot + pending["values"]},
                    )
                if (
                    result["hash"] == pending["values"][-1]["hash"]
                    and type(result["next"]) is int
                    and result["next"] == pending["values"][-1]["record"]["offset"] + 1
                ):
                    synced.append(node)
                    if leader_only and node == leader:
                        return synced
            except (Error, OSError, ValueError, KeyError, TypeError):
                pass
        if len(synced) < len(self.brokers) // 2 + 1:
            raise Error(
                503, "quorum unavailable; outcome pending, retry identical sequence"
            )
        if leader_only and leader not in synced:
            raise Error(503, "leader persistence not confirmed")
        return synced
