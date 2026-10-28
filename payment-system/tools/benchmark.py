"""Small end-to-end loopback measurement; no production capacity claims."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import platform
import time
from lab import Lab, wait

parser = argparse.ArgumentParser()
parser.add_argument("--payments", type=int, default=20)
parser.add_argument("--clients", type=int, default=4)
parser.add_argument("--output", default="benchmarks/latest.json")
args = parser.parse_args()
if not 1 <= args.payments <= 80 or not 1 <= args.clients <= 16:
    parser.error("reference benchmark supports 1..80 payments and 1..16 clients")
with Lab() as lab:
    started = time.perf_counter()

    def pay(index):
        before = time.perf_counter()
        lab.pay(100, "bench_" + str(index))
        return (time.perf_counter() - before) * 1000

    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        latency = sorted(pool.map(pay, range(args.payments)))
    elapsed = time.perf_counter() - started
    lab.settled()
    wait(
        lambda: (
            lab.request("/admin/status", role="provider", auth="admin")[
                "pending_webhooks"
            ]
            == 0
        )
    )
    result = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processes": 4,
        "payments": args.payments,
        "clients": args.clients,
        "elapsed_seconds": elapsed,
        "completed_payins_per_second": args.payments / elapsed,
        "create_checkout_settlement_p50_ms": latency[math.ceil(len(latency) * 0.5) - 1],
        "create_checkout_settlement_p95_ms": latency[
            math.ceil(len(latency) * 0.95) - 1
        ],
        "wallet": lab.request("/v1/wallet"),
        "provider": lab.request("/admin/status", role="provider", auth="admin"),
        "consumer": lab.request("/admin/status", role="receiver", auth="admin"),
        "reconciliation": lab.reconcile(),
        "scope": "Loopback mock provider, SQLite single authority, includes automated checkout; excludes process startup and final API outbox/provider callback/reconciliation drain.",
    }
    assert (
        result["wallet"]["available"] == args.payments * 100
        and result["wallet"]["held"] == 0
    )
    assert result["provider"]["unique_operations"] == args.payments
    assert result["consumer"]["unique_events"] == args.payments
    assert result["reconciliation"]["result"]["matches"]
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
