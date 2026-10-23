from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import time
from urllib.parse import urlencode

from .common import Error, digest, http_json, integer, query


def log_page(config, partition, after, until=None):
    args = {"partition": partition, "after": after}
    if until is not None:
        args["until"] = until
    return http_json(
        config["ingress_url"] + "/internal/log?" + urlencode(args),
        config["internal_token"],
    )


def aggregate_query(config, body):
    q = query(body, time.time())
    meta = http_json(
        config["ingress_url"] + "/internal/status", config["internal_token"]
    )
    urls = config["worker_urls"]
    if set(meta["heads"]) != {str(p) for p in range(len(urls))}:
        raise Error(503, "configured worker topology does not cover the raw log")
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = [
            pool.submit(
                http_json, url + "/internal/totals", config["internal_token"], q
            )
            for url in urls
        ]
        parts = [future.result() for future in futures]
    totals = Counter()
    positions, lag = {}, {}
    closed = True
    watermark = float("inf")
    for p, part in enumerate(parts):
        cp = part["checkpoint"]
        if cp["partition"] != p or cp["lineage"] != meta["lineage"]:
            raise Error(503, "worker lineage/partition not ready")
        totals.update(part["totals"])
        if len(totals) > 10000:
            raise Error(422, "global candidate limit exceeded; narrow range or filters")
        positions[str(p)] = cp["seq"]
        # Source head and worker checkpoint are separate snapshots; zero floors a race.
        lag[str(p)] = max(0, meta["heads"][str(p)] - cp["seq"])
        watermark = min(watermark, cp["watermark"])
        closed &= part["closed"]
    ranked = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))[: q["count"]]
    result = {
        "from": q["from"],
        "to": q["to"],
        "filters": q["filters"],
        "ads": [{"ad_id": ad, "count": count} for ad, count in ranked],
        "lineage": meta["lineage"],
        "checkpoints": positions,
        "lag": lag,
        "watermark": watermark,
        "closed": bool(closed),
        "late_corrections_allowed": True,
        "consistency": "per-partition snapshots; no atomic cross-partition snapshot",
    }
    if "ad_id" in q:
        result.update(ad_id=q["ad_id"], count=totals[q["ad_id"]])
    return result


def reconcile(config):
    """Compare independent batch math against durable, per-worker checkpoint snapshots."""
    meta = http_json(
        config["ingress_url"] + "/internal/status", config["internal_token"]
    )
    if set(meta["heads"]) != {str(p) for p in range(len(config["worker_urls"]))}:
        raise Error(409, "reconciliation requires every source partition")
    results = []
    for partition, url in enumerate(config["worker_urls"]):
        audit = http_json(url + "/internal/audit", config["internal_token"])
        cp = audit["checkpoint"]
        if cp["lineage"] != meta["lineage"] or cp["partition"] != partition:
            raise Error(409, "audit lineage mismatch")
        after, counts, seen = 0, Counter(), set()
        while after < cp["seq"]:
            page = log_page(config, partition, after, cp["seq"])
            if page["lineage"] != cp["lineage"] or not page["entries"]:
                raise Error(409, "raw replay incomplete")
            for item in page["entries"]:
                if item["seq"] != after + 1:
                    raise Error(409, "raw replay offset gap")
                value = item["event"]
                if digest(value) != item["hash"]:
                    raise Error(409, "raw replay checksum mismatch")
                if value["event_id"] not in seen:
                    key = (
                        value["click_timestamp"] // 60 * 60,
                        value["ad_id"],
                        value["user_id"],
                        value["ip"],
                        value["country"],
                    )
                    counts[key] += 1
                    seen.add(value["event_id"])
                after = item["seq"]
        rows = [list(key) + [count] for key, count in sorted(counts.items())]
        expected = digest(rows)
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
        "matches": all(p["matches"] for p in results),
        "partitions": results,
    }


def replay(config, worker, until=None):
    """Rebuild into an explicitly separate database using the same streaming path."""
    head = http_json(
        config["ingress_url"] + "/internal/status", config["internal_token"]
    )["heads"][str(worker.partition)]
    target = head if until is None else min(integer(until, "until", 0, 2**53), head)
    if worker.status()["seq"] > target:
        raise Error(409, "rebuild checkpoint exceeds target")
    # Also bind empty partitions to their raw-log lineage.
    worker.apply(log_page(config, worker.partition, worker.status()["seq"], target))
    while worker.status()["seq"] < target:
        batch = log_page(config, worker.partition, worker.status()["seq"], target)
        if not batch["entries"]:
            raise Error(409, "rebuild cannot reach target")
        worker.apply(batch)
    return worker.audit()
