"""Measure batch admission; delayed memory I/O is synthetic, not a vendor service."""

import argparse
import asyncio
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notification_system.durable import SQLiteMessageQueue  # noqa: E402
from notification_system.models import Channel, ContactInfo, NotificationRequest  # noqa: E402
from notification_system.queue import MessageQueue  # noqa: E402
from notification_system.service import NotificationService  # noqa: E402


class DelayedQueue(MessageQueue):
    """An explicit asynchronous wait models an independently waiting backend."""

    def __init__(self, delay):
        super().__init__()
        self.delay = delay
        self.active = 0
        self.peak = 0

    async def enqueue(self, task):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            await super().enqueue(task)
        finally:
            self.active -= 1


async def sample(kind, count, delay, concurrency):
    with tempfile.TemporaryDirectory(prefix="notification-batch-") as directory:
        queue = (
            SQLiteMessageQueue(Path(directory) / "jobs.db", max_depth=count + 1)
            if kind == "sqlite"
            else DelayedQueue(delay if kind == "delayed_memory" else 0)
        )
        service = NotificationService(queue=queue)
        requests = []
        for index in range(count):
            user = f"user-{index}"
            service.contacts.set(ContactInfo(user_id=user, email=f"{user}@example.org"))
            requests.append(
                NotificationRequest(
                    recipient_id=user,
                    channel=Channel.EMAIL,
                    body=f"message-{index}",
                    event_id=f"event-{index}",
                )
            )
        start = time.perf_counter()
        results = (
            await service.send_batch(requests)
            if concurrency is None
            else await service.send_batch(requests, concurrency=concurrency)
        )
        elapsed = time.perf_counter() - start
        assert len(set(results)) == count and all(results)
        assert queue.depth(Channel.EMAIL) == count
        return {
            "seconds": elapsed,
            "accepted_per_second": count / elapsed,
            "peak_enqueue_calls": getattr(queue, "peak", None),
        }


async def run(args):
    cases = {}
    for kind in ("memory", "delayed_memory", "sqlite"):
        samples = [
            await sample(kind, args.requests, args.delay_ms / 1000, args.concurrency)
            for _ in range(args.repeats)
        ]
        cases[kind] = {
            "samples": samples,
            "median_accepted_per_second": statistics.median(
                item["accepted_per_second"] for item in samples
            ),
        }
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "requests_per_sample": args.requests,
        "repeats": args.repeats,
        "requested_concurrency": args.concurrency,
        "synthetic_queue_delay_ms": args.delay_ms,
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=240)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, default=2.0)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.requests < 1
        or args.repeats < 1
        or not math.isfinite(args.delay_ms)
        or args.delay_ms < 0
        or (args.concurrency is not None and args.concurrency < 1)
    ):
        parser.error("requests/repeats must be positive and delay nonnegative")
    result = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                kind: round(data["median_accepted_per_second"], 2)
                for kind, data in result["cases"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
