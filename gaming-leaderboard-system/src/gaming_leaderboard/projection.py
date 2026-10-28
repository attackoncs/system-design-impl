"""Validated score projection with Redis-atomic epochs and bounded I/O."""

from contextlib import contextmanager
import json
import threading
import time
import uuid

from .common import Error, dumps, http_json, identifier, integer
from .projection_contracts import batch_copy, query_copy
from .projection_scripts import APPLY as APPLY, QUERY as QUERY, RESET as RESET, SNAPSHOT
from .redis import Redis, RedisError
from .transport import loads


class Projection:
    def __init__(self, config, shard):
        self.config = json.loads(dumps(config))
        integer(shard, "shard", 0, len(config["projection_urls"]) - 1)
        self.shard = shard
        self.root = identifier(config["namespace"]) + ":shard:" + str(shard)
        self.clients = [
            Redis(n["host"], n["port"], config["redis_password"])
            for n in config["redis_nodes"][shard]
        ]
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self.state = {"error": "starting", "last_poll": 0}

    @staticmethod
    def _budget(deadline, cancel, default=5):
        end = time.monotonic() + default if deadline is None else deadline
        if (
            type(end) not in (int, float)
            or end != end
            or end in (float("inf"), -float("inf"))
        ):
            raise ValueError("finite monotonic deadline required")
        if end <= time.monotonic() or (cancel is not None and cancel.is_set()):
            raise TimeoutError("projection operation cancelled or deadline exceeded")
        return end

    @contextmanager
    def _writer(self, deadline, cancel):
        while not self.lock.acquire(
            timeout=min(0.05, max(0.001, deadline - time.monotonic()))
        ):
            self._budget(deadline, cancel)
        try:
            self._budget(deadline, cancel)
            yield
        finally:
            self.lock.release()

    def master(self, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        masters = []
        for client in self.clients:
            try:
                role = client.command(
                    "ROLE", timeout=0.2, deadline=deadline, cancel=cancel
                )
                if (
                    not isinstance(role, list)
                    or not role
                    or role[0] not in ("master", "slave")
                ):
                    raise RedisError("invalid Redis role response")
                if role[0] == "master":
                    masters.append(client)
            except (OSError, RedisError):
                pass
        self._budget(deadline, cancel)
        if len(masters) != 1:
            raise Error(
                503,
                "exactly one reachable master required; fence/promote operationally",
            )
        return masters[0]

    def _snapshot(self, client, deadline, cancel):
        result = loads(
            client.command(
                "EVAL", SNAPSHOT, 1, self.root, deadline=deadline, cancel=cancel
            )
        )
        if not isinstance(result, dict) or set(result) != {
            "position",
            "lineage",
            "epoch",
        }:
            raise RedisError("invalid projection snapshot")
        integer(result["position"], "position", 0, 2**53 - 1)
        for field in ("lineage", "epoch"):
            if result[field] is not None:
                identifier(result[field])
        return result

    def status(self, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        result = self._snapshot(
            self.master(deadline=deadline, cancel=cancel), deadline, cancel
        )
        with self._state_lock:
            worker = dict(self.state)
        return {"shard": self.shard, **result, "worker": worker}

    def position(self, client=None, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        return self._snapshot(
            client or self.master(deadline=deadline, cancel=cancel), deadline, cancel
        )["position"]

    def _apply(self, batch, client, snapshot, deadline, cancel):
        value = {**batch, "epoch": snapshot["epoch"], "next_epoch": uuid.uuid4().hex}
        result = loads(
            client.command(
                "EVAL",
                APPLY,
                1,
                self.root,
                dumps(value),
                deadline=deadline,
                cancel=cancel,
            )
        )
        if (
            not isinstance(result, dict)
            or set(result) - {"stale", "position", "epoch"}
            or not {"position", "epoch"} <= set(result)
        ):
            raise RedisError("invalid projection apply receipt")
        integer(result["position"], "position", 0, 2**53 - 1)
        identifier(result["epoch"])
        if not result.get("stale") and result["position"] != batch["after"] + len(
            batch["events"]
        ):
            raise RedisError("projection apply position mismatch")
        return result

    def apply(self, batch, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        batch = batch_copy(batch, self.shard, len(self.config["projection_urls"]))
        with self._writer(deadline, cancel):
            client = self.master(deadline=deadline, cancel=cancel)
            return self._apply(
                batch,
                client,
                self._snapshot(client, deadline, cancel),
                deadline,
                cancel,
            )

    def poll_once(self, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        with self._writer(deadline, cancel):
            client = self.master(deadline=deadline, cancel=cancel)
            snapshot = self._snapshot(client, deadline, cancel)
            after = snapshot["position"]
            batch = batch_copy(
                http_json(
                    self.config["authority_url"]
                    + f"/internal/feed?shard={self.shard}&after={after}",
                    self.config["internal_token"],
                    deadline=deadline,
                    cancel=cancel,
                ),
                self.shard,
                len(self.config["projection_urls"]),
            )
            if batch["after"] != after:
                raise Error(409, "feed cursor disagrees with requested cursor")
            # Always EVAL even for an empty page: bind lineage and epoch atomically
            # and detect a reset after the feed was fetched by another process.
            result = self._apply(batch, client, snapshot, deadline, cancel)
            with self._state_lock:
                self.state.update(
                    error=None,
                    last_poll=time.time(),
                    source_head=batch["head"],
                    position=result["position"],
                    epoch=result["epoch"],
                )
            return batch["head"] > result["position"]

    def run(self, stop):
        while not stop.is_set():
            try:
                busy = self.poll_once(deadline=time.monotonic() + 5, cancel=stop)
                stop.wait(0.005 if busy else 0.03)
            except Exception as exc:
                with self._state_lock:
                    self.state["error"] = str(exc)
                stop.wait(0.1)

    def query(self, body, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        body = query_copy(body, self.shard, len(self.config["projection_urls"]))
        # Each Lua read is atomic and owns its socket; independent reads do not
        # contend on the Python writer lock used by polling and resets.
        result = loads(
            self.master(deadline=deadline, cancel=cancel).command(
                "EVAL",
                QUERY,
                1,
                self.root,
                dumps(body),
                deadline=deadline,
                cancel=cancel,
            )
        )
        if not isinstance(result, dict):
            raise RedisError("invalid projection query result")
        integer(result.get("position"), "position", body["minimum"], 2**53 - 1)
        if (
            result.get("shard") != self.shard
            or result.get("lineage") != body["lineage"]
        ):
            raise RedisError("query projection identity mismatch")
        identifier(result.get("epoch"))
        if "epoch" in body and result["epoch"] != body["epoch"]:
            raise RedisError("query epoch mismatch")
        for key in ("rows", "above", "below"):
            if key in result and result[key] == {}:
                result[key] = []
        return result

    def reset(self, *, deadline=None, cancel=None):
        deadline = self._budget(deadline, cancel)
        epoch = uuid.uuid4().hex
        with self._writer(deadline, cancel):
            actual = self.master(deadline=deadline, cancel=cancel).command(
                "EVAL", RESET, 1, self.root, epoch, deadline=deadline, cancel=cancel
            )
            if actual != epoch:
                raise RedisError("invalid projection reset receipt")
            with self._state_lock:
                self.state["error"] = "rebuilding"
        return {"rebuilding": True, "shard": self.shard, "epoch": epoch}
