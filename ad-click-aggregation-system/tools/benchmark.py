import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import time

from lab import Lab, wait_for
from ad_click.service import reconcile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=3000)
    parser.add_argument("--output", default="benchmarks/results.json")
    args = parser.parse_args()
    if not 200 <= args.events <= 100000:
        parser.error("events must be between 200 and 100000")
    with Lab() as lab:
        end = int(time.time()) // 60 * 60
        events = [{"event_id": f"bench-{i}", "ad_id": "hot" if i % 10 < 7 else f"ad-{i%100}",
                   "click_timestamp": end - 1 - i % 120, "user_id": "u" + str(i % 20),
                   "ip": "127.0.0.1", "country": "US" if i % 3 else "GB"} for i in range(args.events)]
        begin = time.perf_counter()
        batch_ms = []
        for offset in range(0, len(events), 200):
            start = time.perf_counter()
            lab.send(events[offset:offset+200])
            batch_ms.append((time.perf_counter() - start) * 1000)
        published = time.perf_counter()
        wait_for(lab.caught_up, timeout=60)
        processed = time.perf_counter()
        body = {"from": end - 180, "to": end, "count": 100}
        actual = lab.query(body)
        if sum(row["count"] for row in actual["ads"]) != len(events):
            raise AssertionError("query count mismatch")
        def request(_):
            start = time.perf_counter()
            result = lab.query(body)
            assert sum(row["count"] for row in result["ads"]) == len(events)
            return (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            latencies = list(pool.map(request, range(40)))
        query_seconds = time.perf_counter() - start
        audit = reconcile(lab.config)
        positions = lab.call("ingress", "/internal/status")["heads"]
        result = {"python": platform.python_version(), "platform": platform.platform(),
                  "processes": 6, "partitions": 4, "events": len(events), "hot_fraction": 0.7,
                  "publish_seconds": round(published-begin, 3),
                  "publish_events_per_second": round(len(events)/(published-begin), 2),
                  "all_processed_seconds": round(processed-begin, 3),
                  "batch_accept_p95_ms": round(sorted(batch_ms)[max(0, int(len(batch_ms)*0.95)-1)], 3),
                  "query_requests": 40, "query_clients": 4,
                  "query_p50_ms": round(statistics.median(latencies), 3),
                  "query_p95_ms": round(sorted(latencies)[37], 3),
                  "query_requests_per_second": round(40/query_seconds, 2),
                  "partition_counts": positions, "reconciliation_matches": audit["matches"],
                  "sqlite_bytes": sum(p.stat().st_size for p in lab.root.glob("*.sqlite*")),
                  "scope": "Real localhost HTTP ingest + durable partitioned queue + four workers + API; single raw-log owner, no broker replication; fixed-range uncached queries"}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
