"""Coherent monthly leaderboard reads over bounded concurrent shard RPCs."""

from concurrent.futures import TimeoutError as FutureTimeout, wait
from contextlib import contextmanager, nullcontext
import json
import math
import time
from urllib.error import HTTPError

from .common import Error, digest, dumps, http_json, identifier, integer, shard_for
from .profiles import ProfileCache
from .ranking import neighbors, ranks, row, rows, top
from .service_runtime import Runtime


class Service:
    def __init__(self, config, journal):
        self.config, self.journal = json.loads(dumps(config)), journal
        if len(config["projection_urls"]) != journal.shards:
            raise ValueError("projection topology does not match journal")
        self.runtime = Runtime()
        self.profiles = ProfileCache()

    def close(self):
        self.runtime.close()

    def operation(self, *, deadline, cancel=None):
        return self.runtime.operation(deadline=deadline, cancel=cancel)

    def guard(self, method, path):
        # GETs own their source snapshot inside query()/season(). Feed/status
        # deliberately bypass this gate so projectors can satisfy read barriers.
        return self.runtime.guard(True) if method != "GET" else nullcontext()

    def metrics(self):
        with self.runtime.condition:
            readers = len(self.runtime.readers)
        return {
            "profile_cache": self.profiles.stats(),
            "active_readers": readers,
            "operation_capacity": 8,
            "rpc_workers": 16,
        }

    @contextmanager
    def _owned_operation(self):
        if hasattr(self.runtime.local, "deadline"):
            yield
        else:
            with self.operation(deadline=time.monotonic() + 15):
                yield

    @contextmanager
    def _snapshot(self, month):
        while True:
            with self.runtime.guard():
                snapshot = self.journal.read_snapshot(month)
                if snapshot is not None:
                    yield snapshot
                    return
            # No read-to-write upgrade: a first query in a new UTC month prepares
            # its zero-score membership, then recaptures a fresh shared snapshot.
            with self.runtime.guard(True):
                self.journal.snapshot(month)

    def season(self, month):
        with self._owned_operation(), self._snapshot(month) as snapshot:
            return snapshot["season"]

    def _rpc(self, url, body=None, *, deadline=None, cancel=None):
        current, stopping = self.runtime.context()
        return http_json(
            url,
            self.config["internal_token"],
            body,
            deadline=current if deadline is None else deadline,
            cancel=stopping if cancel is None else cancel,
        )

    def _parallel(self, function, items, deadline, cancel):
        self.runtime.remaining(deadline, cancel)
        futures = [self.runtime.pool.submit(function, item) for item in items]
        try:
            return [
                future.result(timeout=self.runtime.remaining(deadline, cancel))
                for future in futures
            ]
        except FutureTimeout:
            raise Error(503, "leaderboard scatter deadline exceeded") from None
        finally:
            for future in futures:
                future.cancel()
            # A local failure must not release snapshot ownership while accepted
            # RPC work is still using it. The real transport interrupts sockets.
            wait(futures)

    def _heads(self, heads):
        count = len(self.config["projection_urls"])
        if not isinstance(heads, dict) or set(heads) != {str(i) for i in range(count)}:
            raise ValueError("complete source heads required")
        return {key: integer(value, "head") for key, value in heads.items()}

    def _envelope(self, result, shard, minimum, epoch=None, *, exact=False):
        if (
            not isinstance(result, dict)
            or type(result.get("shard")) is not int
            or result["shard"] != shard
        ):
            raise Error(502, "projection response shard mismatch")
        position, generation = result.get("position"), result.get("epoch")
        if (
            type(position) is not int
            or not 0 <= position <= 2**53
            or not isinstance(generation, str)
            or not generation
            or len(generation) > 80
        ):
            raise Error(502, "invalid projection watermark")
        if (
            result.get("lineage") != self.journal.lineage
            or position < minimum
            or (exact and position != minimum)
            or (epoch is not None and generation != epoch)
        ):
            raise Error(503, "projection snapshot changed or is not ready")
        return result

    def _watermarks(self, heads, deadline, cancel):
        def status(shard):
            value = self._rpc(
                self.config["projection_urls"][shard] + "/internal/status",
                deadline=deadline,
                cancel=cancel,
            )
            return self._envelope(value, shard, heads[str(shard)])

        return self._parallel(status, range(len(heads)), deadline, cancel)

    def _ready(self, heads, timeout):
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
        ):
            raise ValueError("invalid readiness timeout")
        heads = self._heads(heads)
        outer, cancel = self.runtime.context()
        deadline = min(outer, time.monotonic() + timeout)
        while True:
            try:
                return self._watermarks(heads, deadline, cancel)
            except (Error, OSError, HTTPError):
                if cancel.is_set() or time.monotonic() >= deadline:
                    return None
                # Poll with an interruptible wait; do not extend the total budget.
                self.runtime.closing.wait(
                    min(0.02, max(0, deadline - time.monotonic()))
                )

    def ready(self, heads, timeout=1.5):
        return self._ready(heads, timeout) is not None

    def node(self, shard, body, *, deadline=None, cancel=None):
        value = self._rpc(
            self.config["projection_urls"][shard] + "/internal/query",
            body,
            deadline=deadline,
            cancel=cancel,
        )
        return self._envelope(
            value, shard, body["minimum"], body.get("epoch"), exact=True
        )

    def scatter(self, month, heads, *, epochs=None, **body):
        heads = self._heads(heads)
        deadline, cancel = self.runtime.context()

        def query(shard):
            query_body = {
                "month": month,
                "lineage": self.journal.lineage,
                "minimum": heads[str(shard)],
                **body,
            }
            if epochs is not None:
                query_body["epoch"] = epochs[shard]
            return self.node(shard, query_body, deadline=deadline, cancel=cancel)

        return self._parallel(query, range(len(heads)), deadline, cancel)

    def query(self, month=None, user=None, radius=0):
        integer(radius, "neighbors", 0, 4)
        if user is not None:
            identifier(user)
        with self._owned_operation(), self._snapshot(month) as snapshot:
            season, heads = snapshot["season"], snapshot["heads"]
            month, count = season["month"], len(heads)
            watermarks = self._ready(heads, 1.5)
            if watermarks is None:
                raise Error(
                    503, "ranking unavailable or rebuilding; no partial results"
                )
            epochs = [part["epoch"] for part in watermarks]
            if user is None:
                result = top(
                    self.scatter(month, heads, epochs=epochs, mode="top", limit=10),
                    count,
                )
            else:
                owner = shard_for(user, count)
                target = self.node(
                    owner,
                    {
                        "month": month,
                        "lineage": snapshot["lineage"],
                        "minimum": heads[str(owner)],
                        "epoch": epochs[owner],
                        "mode": "target",
                        "user": user,
                    },
                ).get("target")
                if target is None:
                    raise Error(404, "player is not in this season")
                target = row(target, owner, count)
                if target["user"] != user:
                    raise Error(502, "projection returned another target")
                result, start = neighbors(
                    target,
                    self.scatter(
                        month,
                        heads,
                        epochs=epochs,
                        mode="around",
                        pivot=target["lex"],
                        score=target["score"],
                        radius=radius,
                    ),
                    radius,
                    count,
                )
                values = self.scatter(
                    month,
                    heads,
                    epochs=epochs,
                    mode="counts",
                    scores=sorted({r["score"] for r in result}),
                )
                ranks(result, values, start, count)
            names = self.profiles.names(
                self.journal, snapshot["profile_revision"], [r["user"] for r in result]
            )
            for value in result:
                value.pop("lex", None)
                value["name"] = names[value["user"]]
            return {
                "month": month,
                "season_status": season["status"],
                "rows": result,
                "heads": heads,
                "lineage": snapshot["lineage"],
                "epochs": epochs,
                "ties": "competition rank; user ID ascending display order",
            }

    def reconcile(self, month):
        with self._owned_operation(), self.runtime.guard(True):
            snapshot = self.journal.snapshot(month)
            heads = snapshot["heads"]
            watermarks = self._ready(heads, 1.5)
            if watermarks is None:
                raise Error(503, "projection not ready")
            parts = self.scatter(
                month, heads, epochs=[p["epoch"] for p in watermarks], mode="dump"
            )
            actual = sorted(
                (r["user"], r["score"])
                for shard, part in enumerate(parts)
                for r in rows(part.get("rows"), shard, len(heads), 10000)
            )
            expected = sorted((r["user"], r["score"]) for r in self.journal.rows(month))
            return {
                "month": month,
                "matches": actual == expected,
                "expected_sha256": digest(expected),
                "redis_sha256": digest(actual),
                "players": len(expected),
            }
