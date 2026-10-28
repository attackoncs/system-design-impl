"""Consumer membership, generation fencing, assignments and durable cursors.

The coordinator supplies short SQLite transactions, operation locks and fetch.
Polling waits outside both SQL and partition locks, validating membership again
on every attempt. Cross-partition queue transactions publish through one SQLite
transaction in the owning coordinator.
"""

import uuid

from .common import Error, ZERO, integer, name
from .coordination_locks import partition_key
from .coordinator_validation import tags_filter


class ConsumerGroups:
    def _expire(self, db, topic, group):
        count = db.execute(
            "DELETE FROM members WHERE topic=? AND group_name=? AND expires<=?",
            (topic, group, self._now()),
        ).rowcount
        if count:
            db.execute(
                "UPDATE groups SET generation=generation+1 WHERE topic=? AND name=?",
                (topic, group),
            )
            db.execute(
                "DELETE FROM delivered WHERE topic=? AND group_name=?", (topic, group)
            )

    def _membership(
        self, db, topic, group, member, session, generation=None, partition=None
    ):
        name(topic)
        name(group)
        name(member)
        name(session)
        if generation is not None:
            integer(generation)
        if partition is not None:
            integer(partition, 0, 63)
        self._expire(db, topic, group)
        metadata = db.execute(
            "SELECT generation FROM groups WHERE topic=? AND name=?", (topic, group)
        ).fetchone()
        row = db.execute(
            "SELECT * FROM members WHERE topic=? AND group_name=? AND id=?",
            (topic, group, member),
        ).fetchone()
        if not metadata or not row or row["session"] != session:
            raise Error(409, "consumer session expired or replaced")
        if generation is not None and metadata[0] != generation:
            raise Error(409, "consumer generation fenced; refresh assignments")
        members = [
            r[0]
            for r in db.execute(
                "SELECT id FROM members WHERE topic=? AND group_name=? ORDER BY id",
                (topic, group),
            )
        ]
        options = self.topic(db, topic)
        assigned = [
            p
            for p in range(options["partitions"])
            if members[p % len(members)] == member
        ]
        if partition is not None and partition not in assigned:
            raise Error(409, "consumer no longer owns partition")
        return {
            "generation": metadata[0],
            "assignments": assigned,
            "session": session,
            "member": member,
        }

    def join(self, topic, group, member):
        name(group)
        name(member)
        with self.transaction() as db:
            self.topic(db, topic)
            db.execute("INSERT OR IGNORE INTO groups VALUES(?,?,0)", (topic, group))
            self._expire(db, topic, group)
            session = uuid.uuid4().hex
            db.execute(
                "INSERT INTO members VALUES(?,?,?,?,?) ON CONFLICT(topic,group_name,id) DO UPDATE SET session=excluded.session,expires=excluded.expires",
                (topic, group, member, session, self._now() + self.lease_seconds),
            )
            db.execute(
                "UPDATE groups SET generation=generation+1 WHERE topic=? AND name=?",
                (topic, group),
            )
            db.execute(
                "DELETE FROM delivered WHERE topic=? AND group_name=?", (topic, group)
            )
            return self._membership(db, topic, group, member, session)

    def heartbeat(self, topic, group, member, session):
        with self.transaction() as db:
            result = self._membership(db, topic, group, member, session)
            db.execute(
                "UPDATE members SET expires=? WHERE topic=? AND group_name=? AND id=?",
                (self._now() + self.lease_seconds, topic, group, member),
            )
            return result

    def leave(self, topic, group, member, session):
        with self.transaction() as db:
            self._membership(db, topic, group, member, session)
            db.execute(
                "DELETE FROM members WHERE topic=? AND group_name=? AND id=?",
                (topic, group, member),
            )
            db.execute(
                "UPDATE groups SET generation=generation+1 WHERE topic=? AND name=?",
                (topic, group),
            )
            db.execute(
                "DELETE FROM delivered WHERE topic=? AND group_name=?", (topic, group)
            )
            generation = db.execute(
                "SELECT generation FROM groups WHERE topic=? AND name=?", (topic, group)
            ).fetchone()[0]
        return {"left": True, "generation": generation}

    def expand(self, topic, partitions):
        integer(partitions, 1, 64)
        with self.transaction() as db:
            old = self.topic(db, topic)["partitions"]
            if partitions <= old:
                raise Error(409, "partition expansion must increase the count")
            nodes = sorted(self.brokers)
            db.executemany(
                "INSERT INTO parts VALUES(?,?,?,0,-1,0,?)",
                [
                    (topic, p, nodes[p % len(nodes)], ZERO)
                    for p in range(old, partitions)
                ],
            )
            db.execute(
                "UPDATE topics SET partitions=? WHERE name=?", (partitions, topic)
            )
            db.execute(
                "UPDATE groups SET generation=generation+1 WHERE topic=?", (topic,)
            )
            db.execute("DELETE FROM delivered WHERE topic=?", (topic,))
        return {"topic": topic, "partitions": partitions, "previous_partitions": old}

    def _cursor(self, db, topic, group, partition):
        row = db.execute(
            "SELECT next FROM cursors WHERE topic=? AND group_name=? AND partition=?",
            (topic, group, partition),
        ).fetchone()
        return row[0] if row else 0

    def group_fetch(
        self,
        topic,
        group,
        member,
        session,
        generation,
        partition,
        limit=100,
        semantics="at_least_once",
        tags=None,
        wait_seconds=0,
    ):
        if semantics not in ("at_least_once", "at_most_once"):
            raise ValueError("invalid delivery semantics")
        name(topic)
        integer(partition, 0, 63)
        filters = tags_filter(tags)

        def page():
            with self.operations.hold(partition_key(topic, partition)):
                return self._group_fetch(
                    topic,
                    group,
                    member,
                    session,
                    generation,
                    partition,
                    limit,
                    semantics,
                    filters,
                )

        return self._poll(page, wait_seconds)

    def _group_fetch(
        self,
        topic,
        group,
        member,
        session,
        generation,
        partition,
        limit,
        semantics,
        filters,
    ):
        integer(generation)
        with self.transaction() as db:
            self._membership(db, topic, group, member, session, generation, partition)
            offset = self._cursor(db, topic, group, partition)
        result = self._fetch(topic, partition, offset, limit, filters)
        with self.transaction() as db:
            self._membership(db, topic, group, member, session, generation, partition)
            old = db.execute(
                "SELECT next FROM delivered WHERE topic=? AND group_name=? AND partition=? AND generation=?",
                (topic, group, partition, generation),
            ).fetchone()
            delivered = max(result["next_offset"], old[0] if old else 0)
            db.execute(
                "INSERT INTO delivered VALUES(?,?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET generation=excluded.generation,next=excluded.next",
                (topic, group, partition, generation, delivered),
            )
            if semantics == "at_most_once":
                db.execute(
                    "INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",
                    (topic, group, partition, result["next_offset"]),
                )
        return (
            {**result, "semantics": semantics, "generation": generation},
            result["next_offset"] != offset,
        )

    def _validate_commit(
        self,
        db,
        topic,
        group,
        member,
        session,
        generation,
        partition,
        next_offset,
        expected=None,
    ):
        integer(next_offset)
        integer(generation)
        if expected is not None:
            integer(expected)
        self._membership(db, topic, group, member, session, generation, partition)
        current = self._cursor(db, topic, group, partition)
        bound = db.execute(
            "SELECT next FROM delivered WHERE topic=? AND group_name=? AND partition=? AND generation=?",
            (topic, group, partition, generation),
        ).fetchone()
        if (
            next_offset < current
            or not bound
            or next_offset > bound[0]
            or expected is not None
            and expected != current
        ):
            raise Error(409, "commit exceeds delivery bound or input cursor changed")

    def commit(self, topic, group, member, session, generation, partition, next_offset):
        name(topic)
        integer(partition, 0, 63)
        with self.operations.hold(partition_key(topic, partition)):
            return self._commit(
                topic, group, member, session, generation, partition, next_offset
            )

    def _commit(
        self, topic, group, member, session, generation, partition, next_offset
    ):
        with self.transaction() as db:
            self._validate_commit(
                db, topic, group, member, session, generation, partition, next_offset
            )
            db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",
                (topic, group, partition, next_offset),
            )
        return {"next_offset": next_offset}

    def reset(
        self, topic, group, member, session, generation, partition, offset="earliest"
    ):
        name(topic)
        integer(partition, 0, 63)
        with self.operations.hold(partition_key(topic, partition)):
            return self._reset(
                topic, group, member, session, generation, partition, offset
            )

    def _reset(self, topic, group, member, session, generation, partition, offset):
        integer(generation)
        with self.transaction() as db:
            self._membership(db, topic, group, member, session, generation, partition)
            part = self.part(db, topic, partition)
            target = (
                part["floor"]
                if offset == "earliest"
                else part["hwm"] + 1
                if offset == "latest"
                else integer(offset)
            )
            if not part["floor"] <= target <= part["hwm"] + 1:
                raise Error(416, "reset offset outside retained range")
            db.execute(
                "INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(topic,group_name,partition) DO UPDATE SET next=excluded.next",
                (topic, group, partition, target),
            )
            db.execute(
                "DELETE FROM delivered WHERE topic=? AND group_name=? AND partition=?",
                (topic, group, partition),
            )
            return {"next_offset": target}
