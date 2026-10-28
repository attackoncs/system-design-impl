import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import time

from lab import Lab
from gaming_leaderboard.common import current_month


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/results.json")
    args = parser.parse_args()
    with Lab() as lab:
        for user in [f"player{i:02}" for i in range(20)] + ["loser"]:
            lab.register(user)
        writes = []
        start = time.perf_counter()
        for i in range(60):
            begin = time.perf_counter()
            value = lab.match(f"bench-{i}", f"player{i % 10:02}")
            if not value["projected"]:
                raise AssertionError("benchmark lost real-time projection readiness")
            writes.append((time.perf_counter() - begin) * 1000)
        write_seconds = time.perf_counter() - start

        def measure(path):
            def query(_):
                begin = time.perf_counter()
                result = lab.api(path)
                if not result["rows"]:
                    raise AssertionError("empty measured leaderboard")
                return (time.perf_counter() - begin) * 1000

            begin = time.perf_counter()
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(query, range(40)))
            return {
                "p50_ms": round(statistics.median(values), 3),
                "p95_ms": round(sorted(values)[37], 3),
                "requests_per_second": round(40 / (time.perf_counter() - begin), 2),
            }

        top = measure("/v1/scores")
        rank = measure("/v1/scores/player05?neighbors=4")
        audit = lab.api(
            "/admin/reconcile", {"month": current_month(time.time())}, "admin"
        )
        if not audit["matches"]:
            raise AssertionError("projection reconciliation failed")
        result = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "redis": "8.0.5 (verified local binary)",
            "processes": 7,
            "redis_shards": 2,
            "redis_replicas": 2,
            "players": 21,
            "unique_matches": 60,
            "write_seconds": round(write_seconds, 3),
            "projected_matches_per_second": round(60 / write_seconds, 2),
            "write_p50_ms": round(statistics.median(writes), 3),
            "write_p95_ms": round(sorted(writes)[56], 3),
            "query_requests_per_kind": 40,
            "query_clients": 4,
            "top10": top,
            "rank_with_neighbors": rank,
            "reconciliation_matches": audit["matches"],
            "journal_bytes": sum(p.stat().st_size for p in lab.root.glob("*.sqlite")),
            "scope": "real localhost HTTP + four Redis primary/replica processes with AOF always; synchronous projection barrier, bounded concurrent reads, exclusive authority writes; not 25M-user scale",
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
