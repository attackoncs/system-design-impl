"""Reproducible, network-free URL-frontier scheduling measurements."""

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web_crawler.frontier import URLFrontier
from web_crawler.models import FrontierEntry, Priority


def run(backlog, hosts, polls, repeats):
    samples = {"reserved_host_backlog": [], "distinct_ready_hosts": []}
    for _ in range(repeats):
        frontier = URLFrontier(per_host_delay=0)
        first = FrontierEntry("https://busy.test/active", Priority.HIGH)
        frontier.put(first)
        assert frontier.get(reserve=True) is first
        for index in range(backlog):
            frontier.put(FrontierEntry(f"https://busy.test/{index}", Priority.HIGH))
        for index in range(polls):
            frontier.put(FrontierEntry(f"https://ready-{index}.test/", Priority.LOW))
        start = time.perf_counter()
        for _ in range(polls):
            entry = frontier.get(reserve=True)
            assert entry is not None and entry.host != "busy.test"
        assert frontier.get(reserve=True) is None
        samples["reserved_host_backlog"].append(time.perf_counter() - start)
        assert frontier.size == backlog

        frontier = URLFrontier(per_host_delay=0)
        for index in range(hosts):
            frontier.put(FrontierEntry(f"https://h{index}.test/"))
        start = time.perf_counter()
        for _ in range(hosts):
            assert frontier.get(reserve=True) is not None
        samples["distinct_ready_hosts"].append(time.perf_counter() - start)
        assert frontier.is_empty()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "backlog": backlog,
        "hosts": hosts,
        "polls": polls,
        "repeats": repeats,
        "seconds": samples,
        "median_seconds": {
            name: statistics.median(values) for name, values in samples.items()
        },
        "scope": "In-memory scheduler only; excludes enqueue, HTTP, parsing, SQLite and disk.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backlog", type=int, default=10000)
    parser.add_argument("--hosts", type=int, default=5000)
    parser.add_argument("--polls", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.backlog, args.hosts, args.polls, args.repeats) <= 0:
        parser.error("workload sizes and repeats must be positive")
    result = run(args.backlog, args.hosts, args.polls, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["median_seconds"]))
