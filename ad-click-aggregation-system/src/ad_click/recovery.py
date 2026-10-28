"""Bounded independent batch reconciliation and replay through the streaming path."""

from collections import Counter
import json
import time
from urllib.parse import urlencode

from .common import Error, digest, dumps, event, http_json, integer
from .query_service import checkpoint, finite, metadata


class Remote:
    def __init__(self, config, timeout):
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError("recovery timeout must be in (0,3600]")
        self.config = json.loads(dumps(config))
        self.deadline = time.monotonic() + timeout

    def call(self, url):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recovery budget expired")
        return http_json(
            url,
            self.config["internal_token"],
            timeout=remaining,
            deadline=self.deadline,
        )

    def meta(self):
        return metadata(
            self.config, self.call(self.config["ingress_url"] + "/internal/status")
        )

    def page(self, partition, after, until, lineage):
        args = urlencode({"partition": partition, "after": after, "until": until})
        page = self.call(self.config["ingress_url"] + "/internal/log?" + args)
        try:
            if (
                page["lineage"] != lineage
                or type(page["partition"]) is not int
                or page["partition"] != partition
            ):
                raise ValueError("source identity mismatch")
            integer(page["head"], "head", until, 2**53)
            entries = page["entries"]
            if (
                not isinstance(entries, list)
                or len(entries) > 200
                or (after < until and not entries)
            ):
                raise ValueError("raw replay incomplete")
            expected = after
            for item in entries:
                expected += 1
                if (
                    type(item["seq"]) is not int
                    or item["seq"] != expected
                    or expected > until
                ):
                    raise ValueError("raw replay offset gap")
                finite(item["accepted"])
                normalized = event(item["event"], item["accepted"])
                if normalized != item["event"] or digest(normalized) != item["hash"]:
                    raise ValueError("raw replay checksum mismatch")
        except (KeyError, TypeError, ValueError, OverflowError):
            raise Error(409, "invalid or incomplete raw replay page") from None
        return page


def reconcile(config, *, timeout=60, max_events=200000, max_dimensions=100000):
    """Compare independent batch dimensions with each worker's pinned audit hash.

    A bounded reference job, not a distributed batch engine or an atomic global
    snapshot. Refuse excessive work instead of allocating unbounded dimensions.
    """
    integer(max_events, "reconciliation events", 1, 1000000)
    integer(max_dimensions, "reconciliation dimensions", 1, 1000000)
    remote = Remote(config, timeout)
    meta = remote.meta()
    scanned, results = 0, []
    for partition, url in enumerate(remote.config["worker_urls"]):
        audit = remote.call(url + "/internal/audit")
        cp = checkpoint(audit["checkpoint"], partition, meta["lineage"])
        scanned += cp["seq"]
        if scanned > max_events:
            raise Error(413, "reconciliation event budget exceeded")
        after, counts, seen = 0, Counter(), {}
        while after < cp["seq"]:
            page = remote.page(partition, after, cp["seq"], cp["lineage"])
            for item in page["entries"]:
                value = item["event"]
                fingerprint = item["hash"]
                previous = seen.get(value["event_id"])
                if previous is not None and previous != fingerprint:
                    raise Error(409, "conflicting duplicate in reconciliation source")
                if previous is None:
                    key = (
                        value["click_timestamp"] // 60 * 60,
                        value["ad_id"],
                        value["user_id"],
                        value["ip"],
                        value["country"],
                    )
                    counts[key] += 1
                    if len(counts) > max_dimensions:
                        raise Error(413, "reconciliation dimension budget exceeded")
                    seen[value["event_id"]] = fingerprint
                after = item["seq"]
        expected = digest(
            [list(key) + [count] for key, count in sorted(counts.items())]
        )
        results.append(
            {
                "partition": partition,
                "seq": after,
                "batch_digest": expected,
                "online_digest": audit["digest"],
                "matches": expected == audit["digest"],
                "count": sum(counts.values()),
            }
        )
    return {
        "lineage": meta["lineage"],
        "matches": all(part["matches"] for part in results),
        "partitions": results,
    }


def replay(config, worker, until=None, *, timeout=60, max_events=200000):
    """Rebuild a separate database using bounded verified streaming pages."""
    integer(max_events, "replay events", 1, 1000000)
    remote = Remote(config, timeout)
    meta = remote.meta()
    partition = integer(worker.partition, "partition", 0, len(meta["heads"]) - 1)
    head = meta["heads"][str(partition)]
    target = head if until is None else min(integer(until, "until", 0, 2**53), head)
    current = worker.status()["seq"]
    if current > target:
        raise Error(409, "rebuild checkpoint exceeds target")
    if target - current > max_events:
        raise Error(413, "rebuild event budget exceeded")
    # Bind the lineage even for a completely empty source partition.
    while True:
        page = remote.page(partition, current, target, meta["lineage"])
        worker.apply(page)
        updated = worker.status()["seq"]
        if updated >= target:
            break
        if updated <= current:
            raise Error(409, "rebuild failed to advance")
        current = updated
    return worker.audit()
