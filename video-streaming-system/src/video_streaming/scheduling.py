"""Transactional DAG queue, capability routing, retries and renewable leases."""

import collections
import json
import math
import re
import uuid

from video_streaming.pipeline import KINDS


def duration(value, name="lease", *, zero=False):
    try:
        valid = (
            type(value) in (int, float)
            and math.isfinite(value)
            and (value >= 0 if zero else value > 0)
            and value <= 86400
        )
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {name}")
    return value


def attempts(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("invalid attempt limit")
    return value


class JobQueue:
    """One metadata connection and lock shared with its video state owner.

    Expensive media work runs outside this repository. SQLite BEGIN IMMEDIATE
    or the PostgreSQL adapter transaction serializes cross-instance decisions.
    """

    def __init__(self, db, lock, clock, video, objects):
        self.db, self.lock, self.clock = db, lock, clock
        self.video, self.objects = video, objects

    def enqueue(self, video, pipeline, priority):
        """Insert a generated plan inside the caller's finalization transaction."""
        for task in pipeline.tasks():
            self.db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,'QUEUED',0,0,NULL,0,NULL,NULL,?)",
                (
                    uuid.uuid4().hex,
                    video,
                    task.name,
                    task.kind,
                    task.profile,
                    json.dumps(task.dependencies),
                    priority,
                ),
            )

    def cancel(self, video):
        self.db.execute(
            "UPDATE videos SET state='FAILED' WHERE id=? AND state='PROCESSING'",
            (video,),
        )
        self.db.execute(
            "UPDATE jobs SET state='CANCELLED' WHERE video=? AND state IN ('QUEUED','RUNNING')",
            (video,),
        )

    def _recover(self, now):
        for row in self.db.execute(
            "SELECT j.*,COALESCE(l.attempts,3) AS attempt_limit FROM jobs j "
            "LEFT JOIN job_limits l ON l.job=j.id WHERE j.state='RUNNING' AND j.expires<=?",
            (now,),
        ).fetchall():
            # Pre-upgrade leases had the documented default budget of three.
            self.db.execute(
                "INSERT OR IGNORE INTO job_limits VALUES(?,3)", (row["id"],)
            )
            if row["attempts"] >= row["attempt_limit"]:
                changed = self.db.execute(
                    "UPDATE jobs SET state='FAILED',error='lease attempts exhausted' "
                    "WHERE id=? AND state='RUNNING'",
                    (row["id"],),
                ).rowcount
                if changed:
                    self.cancel(row["video"])
            else:
                self.db.execute(
                    "UPDATE jobs SET state='QUEUED',lease=NULL,due=? "
                    "WHERE id=? AND state='RUNNING'",
                    (now, row["id"]),
                )

    def _ready(self, now, kinds):
        """Walk priority pages with bounded input and per-video dependency caches."""
        suffix, params = "", [now]
        if kinds is not None:
            suffix = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(sorted(kinds))
        sql = (
            "SELECT * FROM jobs WHERE state='QUEUED' AND due<=?"
            + suffix
            + " ORDER BY priority DESC,rowid LIMIT 128 OFFSET ?"
        )
        states, offset = collections.OrderedDict(), 0
        while True:
            rows = self.db.execute(sql, (*params, offset)).fetchall()
            if not rows:
                return
            for row in rows:
                video = row["video"]
                if video not in states:
                    states[video] = {
                        item["name"]: (item["state"], item["output"])
                        for item in self.db.execute(
                            "SELECT name,state,output FROM jobs WHERE video=?", (video,)
                        )
                    }
                    if len(states) > 128:
                        states.popitem(last=False)
                dependencies = json.loads(row["deps"])
                if all(
                    states[video].get(dep, (None,))[0] == "DONE" for dep in dependencies
                ):
                    if self.video(video)["state"] == "PROCESSING":
                        yield (
                            row,
                            {
                                dep: json.loads(states[video][dep][1])
                                for dep in dependencies
                            },
                        )
            offset += len(rows)

    def claim(self, worker, lease_seconds=120, max_attempts=3, *, kinds=None):
        duration(lease_seconds)
        attempts(max_attempts)
        if not isinstance(worker, str) or not 1 <= len(worker) <= 200:
            raise ValueError("invalid worker name")
        if kinds is not None:
            if (
                not isinstance(kinds, (list, tuple, set, frozenset))
                or not kinds
                or any(not isinstance(kind, str) or kind not in KINDS for kind in kinds)
            ):
                raise ValueError("invalid worker capabilities")
            kinds = frozenset(kinds)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            self._recover(now)
            for row, dependencies in self._ready(now, kinds):
                lease = uuid.uuid4().hex
                self.db.execute(
                    "INSERT OR IGNORE INTO job_limits VALUES(?,?)",
                    (row["id"], max_attempts),
                )
                limit = self.db.execute(
                    "SELECT attempts FROM job_limits WHERE job=?", (row["id"],)
                ).fetchone()[0]
                expires = self.clock() + lease_seconds
                self.db.execute(
                    "UPDATE jobs SET state='RUNNING',lease=?,expires=?,attempts=attempts+1 WHERE id=?",
                    (lease, expires, row["id"]),
                )
                job = dict(row)
                job.update(
                    lease=lease,
                    expires=expires,
                    attempts=row["attempts"] + 1,
                    worker=worker,
                    video_data=self.video(row["video"]),
                    dependencies=dependencies,
                    max_attempts=limit,
                )
                return job
        return None

    def _current(self, job):
        current = self.db.execute(
            "SELECT * FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        if (
            current is None
            or current["state"] != "RUNNING"
            or current["lease"] != job["lease"]
            or current["expires"] <= self.clock()
            or self.video(current["video"])["state"] != "PROCESSING"
        ):
            return None
        return current

    def renew(self, job, lease_seconds=120):
        duration(lease_seconds)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._current(job)
            if current is None:
                return False
            self.db.execute(
                "UPDATE jobs SET expires=? WHERE id=?",
                (max(current["expires"], self.clock() + lease_seconds), current["id"]),
            )
            return True

    def complete(self, job, output):
        encoded = json.dumps(output, allow_nan=False)
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._current(job)
            if current is None:
                return False
            if current["kind"] != "inspect":
                if not isinstance(output, dict) or not output:
                    raise ValueError("invalid task output")
                self._validate_assets(current, output)
                if (
                    not isinstance(output, dict)
                    or not output
                    or any(
                        not isinstance(key, str)
                        or re.fullmatch(r"[0-9a-f]{64}", key) is None
                        or not self.objects.file(key).is_file()
                        for key in output.values()
                    )
                ):
                    raise OSError("task output incomplete")
                if current["kind"] == "publish":
                    self._validate_publication(current, output)
            # Slow object operations may have crossed the lease deadline.
            current = self._current(job)
            if current is None:
                return False
            self.db.execute(
                "UPDATE jobs SET state='DONE',output=? WHERE id=?",
                (encoded, current["id"]),
            )
            if current["kind"] == "publish":
                if "master.m3u8" not in output:
                    raise ValueError("master playlist missing")
                self.db.execute(
                    "INSERT OR IGNORE INTO completions VALUES(?,?,0)",
                    (current["video"], encoded),
                )
            return True

    def _validate_publication(self, current, output):
        dependencies = set(json.loads(current["deps"]))
        expected = {}
        for row in self.db.execute(
            "SELECT name,output FROM jobs WHERE video=? AND state='DONE'",
            (current["video"],),
        ):
            if row["name"] in dependencies:
                expected.update(json.loads(row["output"]))
        if set(output) != set(expected) | {"key.bin", "master.m3u8"} or any(
            output[name] != key for name, key in expected.items()
        ):
            raise ValueError("publication does not match completed dependencies")

    def fail(self, job, error, fatal=False, max_attempts=3, retry_base=1):
        attempts(max_attempts)
        duration(retry_base, "retry delay", zero=True)
        if type(fatal) is not bool:
            raise ValueError("fatal must be boolean")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self._current(job)
            if current is None:
                return False
            limit = self.db.execute(
                "SELECT attempts FROM job_limits WHERE job=?", (current["id"],)
            ).fetchone()
            failed = fatal or current["attempts"] >= (limit[0] if limit else 3)
            delay = min(86400, retry_base * 2 ** min(current["attempts"] - 1, 30))
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,due=?,lease=NULL WHERE id=?",
                (
                    "FAILED" if failed else "QUEUED",
                    str(error)[:300],
                    self.clock() + delay,
                    current["id"],
                ),
            )
            if failed:
                self.cancel(current["video"])
            return True

    @staticmethod
    def _validate_assets(current, output):
        names = set(output)
        kind = current["kind"]
        if kind == "thumbnail":
            valid = names == {"thumbnail.png"}
        elif kind == "preprocess":
            valid = all(
                isinstance(name, str) and re.fullmatch(r"gop_[0-9]{5}\.mkv", name)
                for name in names
            )
        elif kind == "encode":
            prefix = str(current["profile"]) + "/"
            valid = (
                prefix + "index.m3u8" in names
                and len(names) >= 2
                and all(
                    isinstance(name, str)
                    and (
                        name == prefix + "index.m3u8"
                        or re.fullmatch(
                            re.escape(prefix) + r"segment_[0-9]{5}\.ts", name
                        )
                    )
                    for name in names
                )
            )
        else:
            valid = {"key.bin", "master.m3u8"} <= names
        if not valid:
            raise ValueError("task output does not match its plan")

    def outputs(self, video):
        with self.lock:
            return {
                row["name"]: json.loads(row["output"])
                for row in self.db.execute(
                    "SELECT name,output FROM jobs WHERE video=? AND state='DONE'",
                    (video,),
                )
            }
