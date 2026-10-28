"""Compare common public operations with a supplied source checkout."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import tracemalloc


def timed(operation, repeats=5):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        operation()
        values.append((time.perf_counter() - start) * 1000)
    return {"median_ms": statistics.median(values), "samples_ms": values}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from consistent_hashing import ConsistentHashRing, affected_ranges

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "source": str(args.source),
        "construction": [],
        "planning": [],
        "lookup": [],
    }
    for count in (10, 100, 400):
        nodes = [f"server-{number}" for number in range(count)]
        report["construction"].append(
            {
                "servers": count,
                "entries": count * 25,
                **timed(lambda: ConsistentHashRing(nodes, 25)),
            }
        )
    for size in (1000, 10000, 50000):
        ring = ConsistentHashRing(
            [f"server-{number}" for number in range(100)], size // 100
        )
        before = ring.snapshot()
        ring.add_node("new", size // 100)
        after = ring.snapshot()
        report["planning"].append(
            {
                "before_entries": size,
                "changed_intervals": len(affected_ranges(before, after)),
                **timed(lambda: affected_ranges(before, after)),
            }
        )
    ring = ConsistentHashRing([f"server-{number}" for number in range(100)], 100)
    for operation in ("add_node", "remove_node"):
        measurements = []
        for _ in range(10):
            if operation == "remove_node":
                ring.add_node("temporary", 100)
            start = time.perf_counter()
            getattr(ring, operation)("temporary")
            measurements.append((time.perf_counter() - start) * 1000)
            if operation == "add_node":
                ring.remove_node("temporary")
        report[operation] = {
            "median_ms": statistics.median(measurements),
            "samples_ms": measurements,
        }
    keys = [f"key-{number}" for number in range(10000)]
    for mode in ("live", "snapshot"):
        view = ring if mode == "live" else ring.snapshot()
        for clients in (1, 8):

            def batch(offset):
                for key in keys[offset::clients]:
                    view.get_node(key)

            def run():
                if clients == 1:
                    batch(0)
                else:
                    with ThreadPoolExecutor(clients) as pool:
                        list(pool.map(batch, range(clients)))

            result = timed(run)
            report["lookup"].append(
                {
                    "mode": mode,
                    "clients": clients,
                    "keys": len(keys),
                    "keys_per_second": len(keys) / (result["median_ms"] / 1000),
                    **result,
                }
            )
    tracemalloc.start()
    retained = ConsistentHashRing([f"server-{number}" for number in range(100)], 100)
    assert retained.total_virtual_nodes == 10000
    report["construction_peak_bytes"] = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({key: report[key] for key in ("construction_peak_bytes", "source")})
    )


if __name__ == "__main__":
    main()
