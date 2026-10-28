"""Measure Merkle updates and actual three-node gRPC operations separately."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time
from pathlib import Path


def percentile(values, fraction):
    return sorted(values)[min(len(values) - 1, int(len(values) * fraction))]


def tree_benchmark(count, buckets):
    from kv_store.cluster.merkle_tree import MerkleTree

    tree = MerkleTree(buckets)
    durations = []
    started = time.perf_counter()
    for index in range(count):
        start = time.perf_counter()
        tree.update(f"key-{index}", f"value-{index}")
        durations.append((time.perf_counter() - start) * 1000)
    elapsed = time.perf_counter() - started
    return {
        "updates": count,
        "buckets": buckets,
        "seconds": elapsed,
        "updates_per_second": count / elapsed,
        "p50_ms": statistics.median(durations),
        "p95_ms": percentile(durations, 0.95),
        "root": tree.get_root_hash(),
    }


async def runtime_benchmark(count):
    from lab import ClusterLab

    from kv_store.replication.quorum import ConsistencyLevel

    async with ClusterLab(size=3, buckets=1024, memtable_size=64 * 1024) as lab:
        nodes = lab.nodes
        gate = asyncio.Semaphore(8)
        payload = b"x" * 2048

        async def operation(index, write):
            async with gate:
                started = time.perf_counter()
                if write:
                    result = await nodes[index % 3].put(
                        f"key-{index}", payload, consistency=ConsistencyLevel.ALL
                    )
                    assert result.success
                else:
                    result = await nodes[index % 3].get(
                        f"key-{index}", consistency=ConsistencyLevel.ALL
                    )
                    assert result.found and result.value == payload
                return (time.perf_counter() - started) * 1000

        result = {}
        for name, write in (("write", True), ("read", False)):
            start = time.perf_counter()
            durations = await asyncio.gather(
                *(operation(index, write) for index in range(count))
            )
            elapsed = time.perf_counter() - start
            result[name] = {
                "operations": count,
                "clients": 8,
                "replicas": 3,
                "consistency": "all",
                "ops_per_second": count / elapsed,
                "p50_ms": statistics.median(durations),
                "p95_ms": percentile(durations, 0.95),
            }
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--updates", type=int, default=2000)
    parser.add_argument("--operations", type=int, default=120)
    args = parser.parse_args()
    if args.updates <= 0 or args.operations <= 0:
        parser.error("counts must be positive")
    sys.path.insert(0, str(args.source.resolve()))
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "tree": tree_benchmark(args.updates, 4096),
        "runtime": asyncio.run(runtime_benchmark(args.operations)),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
