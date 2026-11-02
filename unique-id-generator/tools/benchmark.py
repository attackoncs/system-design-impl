"""Measure real-clock generation; validate uniqueness outside timed sections."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unique_id import SnowflakeConfig, SnowflakeGenerator


def generate_many(generator: SnowflakeGenerator, count: int) -> list[int]:
    return [generator.generate() for _ in range(count)]


def validate(groups: list[list[int]]) -> int:
    values = [value for group in groups for value in group]
    assert all(all(a < b for a, b in zip(group, group[1:])) for group in groups)
    assert all(0 <= value < 1 << 63 for value in values)
    assert len(values) == len(set(values))
    return len(values)


def sample(mode: str, count: int, clients: int) -> dict:
    generator = SnowflakeGenerator()
    started = time.perf_counter()
    if mode == "single":
        groups = [generate_many(generator, count)]
    elif mode == "batch":
        groups = [
            generator.generate_batch(min(1000, count - offset))
            for offset in range(0, count, 1000)
        ]
    else:
        with ThreadPoolExecutor(max_workers=clients) as pool:
            groups = list(
                pool.map(
                    lambda _: generate_many(generator, count // clients), range(clients)
                )
            )
    elapsed = time.perf_counter() - started
    verified = validate(groups)
    # Batch boundaries also share one generator and must be strictly increasing.
    if mode == "batch":
        assert all(a[-1] < b[0] for a, b in zip(groups, groups[1:]))
    return {"ids": verified, "seconds": elapsed, "ids_per_second": verified / elapsed}


def process_worker(work: tuple[int, int]) -> tuple[list[int], dict]:
    identity, count = work
    generator = SnowflakeGenerator(SnowflakeConfig(machine_id=identity))
    started = time.perf_counter()
    values = generate_many(generator, count)
    elapsed = time.perf_counter() - started
    return values, {
        "pid": os.getpid(),
        "machine_id": identity,
        "ids": count,
        "seconds": elapsed,
        "ids_per_second": count / elapsed,
    }


def run(count: int, clients: int, repeats: int) -> dict:
    scenarios = {}
    for mode in ("single", "batch", "shared_threads"):
        samples = [sample(mode, count, clients) for _ in range(repeats)]
        scenarios[mode] = {
            "samples": samples,
            "median_ids_per_second": statistics.median(
                s["ids_per_second"] for s in samples
            ),
        }
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=clients) as pool:
        results = list(
            pool.map(process_worker, [(i, count // clients) for i in range(clients)])
        )
    elapsed = time.perf_counter() - started
    verified = validate([values for values, _ in results])
    workers = [metadata for _, metadata in results]
    scenarios["independent_processes"] = {
        "ids": verified,
        "seconds_including_startup": elapsed,
        "ids_per_second_including_startup": verified / elapsed,
        "workers": workers,
        "distinct_pids": len({worker["pid"] for worker in workers}),
    }
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "clock": "SystemClock",
        "clients": clients,
        "repeats": repeats,
        "batch_size": 1000,
        "scenarios": scenarios,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100000)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        not 1 <= args.clients <= 32
        or args.count < args.clients
        or args.count % args.clients
        or args.repeats < 1
    ):
        parser.error(
            "positive count/repeats required; clients must be 1..32 and divide count"
        )
    report = run(args.count, args.clients, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                mode: data.get(
                    "median_ids_per_second",
                    data.get("ids_per_second_including_startup"),
                )
                for mode, data in report["scenarios"].items()
            },
            indent=2,
        )
    )
