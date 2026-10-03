"""Configurable local trie/cache and real HTTP latency measurement, not a scale claim."""
import argparse
import json
import os
from pathlib import Path
import platform
import time
from threading import Thread
from urllib.request import urlopen

from search_autocomplete import Autocomplete, Store
from search_autocomplete.http import server


def word(index):
    value = []
    while True:
        value.append(chr(97 + index % 26))
        index //= 26
        if not index:
            return "term" + "".join(reversed(value)).rjust(5, "a")


def quantiles(values):
    values = sorted(values)
    return {f"p{p}_ms": round(values[int((len(values) - 1) * p / 100)] * 1000, 4) for p in (50, 95, 99)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=10000)
    parser.add_argument("--requests", type=int, default=10000)
    parser.add_argument("--http-requests", type=int, default=500)
    parser.add_argument("--output")
    args = parser.parse_args()
    if min(args.queries, args.requests, args.http_requests) < 1 or args.queries > 26 ** 5:
        parser.error("positive counts required; queries <= 26^5")
    store = Store()
    http = thread = None
    try:
        for index in range(args.queries):
            store.record(str(index), word(index), 1)
        started = time.perf_counter()
        store.build(0, 2, 4)
        build_seconds = time.perf_counter() - started
        service = Autocomplete(store)
        started = time.perf_counter()
        assert service.suggest("term")
        cold_ms = (time.perf_counter() - started) * 1000
        timings = []
        started = time.perf_counter()
        for index in range(args.requests):
            prefix = word(index % args.queries)[:-1]
            before = time.perf_counter()
            assert service.suggest(prefix)
            timings.append(time.perf_counter() - before)
        request_seconds = time.perf_counter() - started
        http = server(service)
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{http.server_address[1]}/suggest?prefix=term"
        network = []
        for _ in range(args.http_requests):
            before = time.perf_counter()
            with urlopen(url, timeout=5) as response:
                assert len(json.load(response)["suggestions"]) == min(5, args.queries)
            network.append(time.perf_counter() - before)
        report = {"queries": args.queries, "local_requests": args.requests,
                  "http_requests": args.http_requests, "shards": len(store.snapshot()["shards"]),
                  "build_seconds": round(build_seconds, 4), "cold_query_ms": round(cold_ms, 4),
                  "local_latency": quantiles(timings), "http_latency": quantiles(network),
                  "local_requests_per_second": round(args.requests / request_seconds, 2),
                  "environment": {"platform": platform.platform(), "python": platform.python_version(),
                                  "logical_cpus": os.cpu_count()},
                  "scope": "single-host, sequential requests; HTTP service contains all shards"}
        print(json.dumps(report, indent=2))
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    finally:
        if http:
            http.shutdown()
            http.server_close()
            thread.join(timeout=5)
        store.close()


if __name__ == "__main__":
    main()
