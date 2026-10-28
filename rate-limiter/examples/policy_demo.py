"""File publication, request selection, metrics and bounded FIFO pacing."""

import json
from pathlib import Path
from types import SimpleNamespace

from rate_limiter import RateLimiter, RateLimiterConfig
from rate_limiter.queue import LeakyBucketQueue
from rate_limiter.rule_files import RuleReloader


def main():
    limiter = RateLimiter(RateLimiterConfig(fail_open=False))
    request = SimpleNamespace(path="/api/orders", user=SimpleNamespace(id="demo"))
    with RuleReloader(limiter, Path(__file__).with_name("rules.json")) as reloader:
        allowed = [limiter.check_request(request).allowed for _ in range(5)]
        assert allowed == [True, True, True, False, False]
        rules = reloader.status()
    with LeakyBucketQueue(capacity=3, rate=50) as worker:
        futures = [worker.submit(lambda n=n: n) for n in range(3)]
        completed = [future.result(timeout=2) for future in futures]
    return {
        "allowed": allowed,
        "metrics": limiter.metrics(),
        "rules": rules,
        "fifo": completed,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
