"""Bounded parallel fan-out and exact reduction of every worker candidate."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import heapq
import json
import math
import threading
import time
from urllib.error import HTTPError

from .common import Error, dumps, http_json, integer, query, text


def finite(value):
    if (
        type(value) not in (int, float)
        or not 0 <= value <= 2**53
        or not math.isfinite(value)
    ):
        raise ValueError("invalid finite timestamp")
    return value


def metadata(config, body):
    if not isinstance(body, dict) or not isinstance(body.get("heads"), dict):
        raise Error(503, "raw log metadata unavailable")
    lineage = text(body.get("lineage"), "lineage")
    urls = config["worker_urls"]
    if (
        not isinstance(urls, list)
        or not 1 <= len(urls) <= 64
        or len(set(urls)) != len(urls)
    ):
        raise ValueError("one distinct worker URL required per partition")
    if set(body["heads"]) != {str(p) for p in range(len(urls))}:
        raise Error(503, "worker topology does not cover the raw log")
    heads = {
        key: integer(value, "head", 0, 2**53) for key, value in body["heads"].items()
    }
    return {"lineage": lineage, "heads": heads}


def checkpoint(value, partition, lineage):
    if (
        not isinstance(value, dict)
        or type(value.get("partition")) is not int
        or value["partition"] != partition
        or value.get("lineage") != lineage
    ):
        raise Error(503, "worker lineage/partition not ready")
    integer(value["seq"], "checkpoint", 0, 2**53)
    finite(value["watermark"])
    return value


class QueryService:
    def __init__(self, config, *, workers=8, capacity=8, timeout=5):
        integer(workers, "query workers", 1, 64)
        integer(capacity, "query capacity", 1, 64)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("query timeout must be in (0,120]")
        self.config = json.loads(dumps(config))
        urls = self.config.get("worker_urls")
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 64
            or any(not isinstance(url, str) for url in urls)
            or len(set(urls)) != len(urls)
        ):
            raise ValueError("distinct worker URLs required")
        self.timeout = timeout
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ad-query"
        )
        self._slots = threading.BoundedSemaphore(capacity)
        self._condition = threading.Condition()
        self._active, self._closed = 0, False

    def aggregate(self, body):
        q = query(json.loads(dumps(body)), time.time())
        with self._condition:
            if self._closed:
                raise RuntimeError("query service closed")
            if not self._slots.acquire(blocking=False):
                raise Error(503, "query capacity reached")
            self._active += 1
        deadline, cancel, futures = (
            time.monotonic() + self.timeout,
            threading.Event(),
            [],
        )
        try:

            def call(url, payload=None):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or cancel.is_set():
                    raise TimeoutError("query deadline exceeded")
                try:
                    return http_json(
                        url,
                        self.config["internal_token"],
                        payload,
                        timeout=remaining,
                        deadline=deadline,
                        cancel=cancel,
                    )
                except HTTPError as exc:
                    status = exc.code
                    exc.close()
                    if payload is not None and status == 422:
                        raise Error(
                            422,
                            "worker candidate budget exceeded; narrow range or filters",
                        ) from None
                    raise OSError("required peer rejected request") from None

            meta = metadata(
                self.config, call(self.config["ingress_url"] + "/internal/status")
            )
            futures = [
                self._executor.submit(call, url + "/internal/totals", q)
                for url in self.config["worker_urls"]
            ]
            parts = [
                future.result(timeout=max(0.001, deadline - time.monotonic()))
                for future in futures
            ]
            return self._reduce(q, meta, parts)
        except Error:
            raise
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            FutureTimeoutError,
        ):
            raise Error(503, "required source or worker unavailable") from None
        finally:
            cancel.set()
            for future in futures:
                future.cancel()
            # Retain admission until real accepted work exits; a socket deadline
            # cannot kill DNS/SQL/CPU work. Queue length stays bounded by capacity.
            for future in futures:
                try:
                    future.result()
                except BaseException:
                    pass
            with self._condition:
                self._active -= 1
                self._slots.release()
                self._condition.notify_all()

    @staticmethod
    def _reduce(q, meta, parts):
        totals, positions, lag = Counter(), {}, {}
        watermark = float("inf")
        for partition, part in enumerate(parts):
            if (
                not isinstance(part, dict)
                or not isinstance(part.get("totals"), dict)
                or len(part["totals"]) > 10000
            ):
                raise Error(503, "invalid worker totals")
            cp = checkpoint(part["checkpoint"], partition, meta["lineage"])
            for ad_id, count in part["totals"].items():
                text(ad_id, "ad_id")
                integer(count, "count", 0, 2**53)
                if "ad_id" in q and ad_id != q["ad_id"]:
                    raise Error(503, "worker returned an unrelated ad")
                totals[ad_id] += count
            if len(totals) > 10000:
                raise Error(
                    422, "global candidate limit exceeded; narrow range or filters"
                )
            positions[str(partition)] = cp["seq"]
            lag[str(partition)] = max(0, meta["heads"][str(partition)] - cp["seq"])
            watermark = min(watermark, cp["watermark"])
        ranked = heapq.nsmallest(
            q["count"], totals.items(), key=lambda pair: (-pair[1], pair[0])
        )
        result = {
            "from": q["from"],
            "to": q["to"],
            "filters": q["filters"],
            "ads": [{"ad_id": ad, "count": count} for ad, count in ranked],
            "lineage": meta["lineage"],
            "checkpoints": positions,
            "lag": lag,
            "watermark": watermark,
            "closed": q["to"] <= watermark,
            "late_corrections_allowed": True,
            "consistency": "per-partition snapshots; no atomic cross-partition snapshot",
        }
        if "ad_id" in q:
            result.update(ad_id=q["ad_id"], count=totals[q["ad_id"]])
        return result

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.wait_for(lambda: self._active == 0)
        self._executor.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
