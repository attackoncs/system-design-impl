import argparse
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from proximity.store import Store, canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--businesses", type=int, default=10000)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--output", default=".runtime/benchmark.json")
    args = parser.parse_args()
    if args.businesses < 2 or args.queries < 2:
        parser.error("businesses and queries must be >= 2")
    rng = random.Random(17)
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / "benchmark.sqlite")
        with store.transaction(True) as db:
            for i in range(args.businesses):
                dense = i < args.businesses // 2
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
        started = time.perf_counter()
        store.publish(force=True)
        publication = time.perf_counter() - started
        records = []
        for label, lat, lon in [("dense", 31.23, 121.47), ("sparse", 45, -100)]:
            for radius in [500, 5000, 20000]:
                samples = []
                started = time.perf_counter()
                store.search(lat, lon, radius)
                cold_ms = (time.perf_counter() - started) * 1000
                for _ in range(args.queries):
                    started = time.perf_counter()
                    result = store.search(lat, lon, radius)
                    samples.append((time.perf_counter() - started) * 1000)
                records.append(
                    {
                        "dataset": label,
                        "radius_m": radius,
                        "cold_ms": round(cold_ms, 3),
                        "warm_p50_ms": round(statistics.median(samples), 3),
                        "warm_p95_ms": round(
                            sorted(samples)[int(0.95 * (len(samples) - 1))], 3
                        ),
                        "sequential_qps": round(1000 / statistics.mean(samples), 2),
                        "total": result["total"],
                        "candidates": result["candidates"],
                        "cells": result["cells"],
                        "precision": result["precision"],
                    }
                )
        report = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "businesses": args.businesses,
            "queries_per_case": args.queries,
            "publication_seconds": round(publication, 3),
            "sqlite_bytes": sum(
                p.stat().st_size for p in Path(folder).glob("benchmark.sqlite*")
            ),
            "scope": "in-process sequential search, synthetic data; no HTTP or production-scale claim",
            "results": records,
        }
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
