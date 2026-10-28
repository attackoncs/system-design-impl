import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import time

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent),
]
from lab import Cluster, request
from proximity.store import Store, canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=120)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--output", default=".runtime/http-benchmark.json")
    args = parser.parse_args()
    if args.requests < 6 or not 1 <= args.clients <= 32:
        parser.error("requests >= 6 and clients 1..32 required")
    rng = random.Random(17)
    with Cluster() as cluster:
        store = Store(cluster.root / "primary.sqlite")
        with store.transaction(True) as db:
            for i in range(10000):
                dense = i < 5000
                body = {
                    "name": f"Business {i}",
                    "address": "synthetic",
                    "category": "cafe",
                    "hours": [],
                    "latitude": 31.23 + rng.uniform(-0.03, 0.03)
                    if dense
                    else rng.uniform(-80, 80),
                    "longitude": 121.47 + rng.uniform(-0.03, 0.03)
                    if dense
                    else rng.uniform(-180, 180),
                }
                db.execute(
                    "INSERT INTO drafts VALUES(?,?,?,0)",
                    (str(i), "seed", canonical(body)),
                )
        store.publish(force=True)
        primary = cluster.start("primary", "primary")
        cases = [
            (label, lat, lon, radius)
            for label, lat, lon in [("dense", 31.23, 121.47), ("sparse", 45, -100)]
            for radius in [500, 5000, 20000]
        ]

        def query(index):
            label, lat, lon, radius = cases[index % 6]
            started = time.perf_counter()
            try:
                result = request(
                    primary,
                    f"/v1/search/nearby?latitude={lat}&longitude={lon}&radius={radius}",
                )
                return {
                    "case": label + "-" + str(radius),
                    "ms": (time.perf_counter() - started) * 1000,
                    "total": result["total"],
                    "error": False,
                }
            except Exception:
                return {
                    "case": label + "-" + str(radius),
                    "ms": (time.perf_counter() - started) * 1000,
                    "error": True,
                }

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.clients) as executor:
            results = list(executor.map(query, range(args.requests)))
        elapsed = time.perf_counter() - started
        report = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "scope": "localhost real HTTP, independent primary process; synthetic 10,000 businesses, mixed dense/sparse/radius queries, default page limit 100",
            "requests": args.requests,
            "clients": args.clients,
            "seconds": round(elapsed, 3),
            "qps": round(args.requests / elapsed, 2),
            "errors": sum(r["error"] for r in results),
            "cases": [],
        }
        for label, lat, lon, radius in cases:
            name = label + "-" + str(radius)
            samples = [r["ms"] for r in results if r["case"] == name]
            report["cases"].append(
                {
                    "case": name,
                    "requests": len(samples),
                    "p50_ms": round(statistics.median(samples), 3),
                    "p95_ms": round(sorted(samples)[int(0.95 * (len(samples) - 1))], 3),
                }
            )
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        if report["errors"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
