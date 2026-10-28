"""Repeatable in-process measurements; never a production capacity claim."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def measure(checks, clients, algorithm):
    from rate_limiter.config import Algorithm, RateLimiterConfig, RateLimitRule
    from rate_limiter.core import RateLimiter

    limiter = RateLimiter(
        RateLimiterConfig(
            rules=[RateLimitRule(100_000, 3600, Algorithm(algorithm))], fail_open=False
        )
    )

    def check(index):
        start = time.perf_counter()
        result = limiter.check_request(f"subject-{index % 256}")
        return (time.perf_counter() - start) * 1000, result.allowed

    for index in range(256):
        check(index)
    start = time.perf_counter()
    if clients == 1:
        results = [check(index) for index in range(checks)]
    else:
        with ThreadPoolExecutor(clients) as pool:
            results = list(pool.map(check, range(checks)))
    elapsed = time.perf_counter() - start
    assert all(allowed for _, allowed in results)
    durations = sorted(duration for duration, _ in results)
    return {
        "algorithm": algorithm,
        "clients": clients,
        "checks": checks,
        "elapsed_seconds": elapsed,
        "checks_per_second": checks / elapsed,
        "p50_ms": statistics.median(durations),
        "p95_ms": durations[int(len(durations) * 0.95)],
        "p99_ms": durations[int(len(durations) * 0.99)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--checks", type=int, default=10_000)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.checks < 100:
        parser.error("checks must be >= 100")
    sys.path.insert(0, str(args.source.resolve()))
    from rate_limiter.config import Algorithm

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "source": str(args.source),
        "measurements": [],
    }
    for clients in (1, 8):
        for algorithm in Algorithm:
            report["measurements"].append(
                measure(args.checks, clients, algorithm.value)
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
