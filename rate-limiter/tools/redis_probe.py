"""Exercise real Redis across processes and two actual HTTP middleware servers.

Requires an already running disposable Redis; never flushes or shuts it down.
Only keys in a unique probe namespace are deleted in the finally block.
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import redis
from flask import Flask
from werkzeug.serving import make_server

from rate_limiter.config import Algorithm, RateLimiterConfig, RateLimitRule
from rate_limiter.core import RateLimiter
from rate_limiter.middleware.flask import RateLimitExtension
from rate_limiter.storage.redis import RedisStorage

_barrier = None


def initialize_worker(barrier):
    global _barrier
    _barrier = barrier


class SkewedClientStorage(RedisStorage):
    def __init__(self, url, skew):
        super().__init__(url)
        self.skew = skew

    def execute_algorithm(self, lua_script, memory_script, keys, args):
        args = list(args)
        args[2] = str(time.time() + self.skew)
        return super().execute_algorithm(lua_script, memory_script, keys, args)


def worker(arguments):
    url, namespace, name, skew = arguments
    storage = SkewedClientStorage(url, skew)
    limiter = RateLimiter(
        RateLimiterConfig(key_prefix=namespace, fail_open=False), storage=storage
    )
    rule = RateLimitRule(25, 3600, Algorithm(name))
    try:
        _barrier.wait(timeout=20)
        results = [limiter.check("shared-subject", rule) for _ in range(32)]
        return {
            "pid": os.getpid(),
            "allowed": sum(result.allowed for result in results),
            "errors": limiter.metrics()["storage_errors"],
        }
    finally:
        storage.close()


def http_check(url):
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=5) as response:
            return response.status, dict(response.headers)
    except HTTPError as error:
        with error:
            return error.code, dict(error.headers)


def probe(url):
    namespace = "rl_probe_" + uuid.uuid4().hex + ":"
    client = redis.Redis.from_url(url, socket_timeout=2)
    servers, threads, stores = [], [], []
    report = {"redis_version": client.info("server")["redis_version"], "algorithms": {}}
    started = time.perf_counter()
    try:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=4,
            mp_context=context,
            initializer=initialize_worker,
            initargs=(context.Barrier(4),),
        ) as pool:
            for algorithm in Algorithm:
                results = list(
                    pool.map(
                        worker,
                        [
                            (url, namespace, algorithm.value, skew)
                            for skew in (-1e8, 0, 1e8, 9e8)
                        ],
                    )
                )
                count = sum(result["allowed"] for result in results)
                errors = sum(result["errors"] for result in results)
                processes = len({result["pid"] for result in results})
                assert count == 25 and errors == 0 and processes == 4, (
                    algorithm,
                    count,
                    errors,
                    processes,
                )
                report["algorithms"][algorithm.value] = {
                    "requests": 128,
                    "processes": processes,
                    "allowed": count,
                    "storage_errors": errors,
                }
        report["bucket_capacity"] = {}
        for algorithm in (Algorithm.TOKEN_BUCKET, Algorithm.LEAKING_BUCKET):
            storage = RedisStorage(url)
            try:
                limiter = RateLimiter(
                    RateLimiterConfig(key_prefix=namespace + "burst:", fail_open=False),
                    storage=storage,
                )
                rule = RateLimitRule(2, 10, algorithm, capacity=5)
                assert all(limiter.check("burst", rule).allowed for _ in range(5))
                denied = limiter.check("burst", rule)
                assert (
                    not denied.allowed
                    and denied.limit == 5
                    and 0 < denied.retry_after <= 5
                )
                assert limiter.metrics()["storage_errors"] == 0
                report["bucket_capacity"][algorithm.value] = {
                    "capacity": 5,
                    "steady_requests_per_10s": 2,
                    "allowed": 5,
                    "retry_seconds": denied.retry_after,
                }
            finally:
                storage.close()
        for index in range(2):
            app = Flask(f"probe-{index}")
            storage = RedisStorage(url)
            stores.append(storage)
            limiter = RateLimiter(
                RateLimiterConfig(
                    key_prefix=namespace + "http:",
                    fail_open=False,
                    rules=[RateLimitRule(9, 3600)],
                ),
                storage=storage,
            )
            RateLimitExtension(app, limiter=limiter)
            app.add_url_rule("/limited", view_func=lambda: {"ok": True})
            server = make_server("127.0.0.1", 0, app, threaded=True)
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=False)
            thread.start()
            threads.append(thread)
        urls = [f"http://127.0.0.1:{server.server_port}/limited" for server in servers]
        with ThreadPoolExecutor(8) as pool:
            replies = list(pool.map(http_check, [urls[n % 2] for n in range(32)]))
        assert sum(status == 200 for status, _ in replies) == 9
        assert sum(status == 429 for status, _ in replies) == 23
        assert all(headers.get("Retry-After", "1").isdigit() for _, headers in replies)
        report["http"] = {"servers": 2, "requests": 32, "allowed": 9, "denied": 23}
        keys = list(client.scan_iter(match=namespace + "*"))
        assert keys and all(client.ttl(key) > 0 for key in keys)
        report["keys_have_ttl"] = True
        report["elapsed_seconds"] = time.perf_counter() - started
        return report
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
        for storage in stores:
            storage.close()
        try:
            keys = list(client.scan_iter(match=namespace + "*"))
            if keys:
                client.delete(*keys)
        finally:
            client.close()


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = probe(args.url)
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
