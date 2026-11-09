"""Measure one shard's write, snapshot and restart cost as history grows.

Synthetic funds only. Run from the project root with PYTHONPATH=src; pass
--src to compare another checkout of ``digital_wallet``.
"""

import argparse
import json
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--events", type=int, nargs="+", default=[1000, 3000])
parser.add_argument("--tail", type=int, default=10)
parser.add_argument("--src", default=None, help="alternate src directory")
parser.add_argument("--root", default=None, help="data directory (default: temp)")
parser.add_argument("--output", default=None)
args = parser.parse_args()
if args.src:
    sys.path.insert(0, args.src)

from digital_wallet.common import shard_for  # noqa: E402
from digital_wallet.shard import Shard  # noqa: E402

names = [k for k in ("bench_%d" % i for i in range(60)) if shard_for(k, 2) == 0][:2]
accounts = {names[0]: 10**9, names[1]: 0}


def transfer(shard, i):
    body = {
        "transaction_id": "t%d" % i,
        "from_account": names[0],
        "to_account": names[1],
        "amount": 1,
        "currency": "USD",
    }
    shard.action(body, "local", "local", 1, "coordinator")


def measure(count):
    root = Path(tempfile.mkdtemp(prefix="wallet-snapshot-", dir=args.root))
    try:
        path = root / "shard.sqlite"
        shard = Shard(path, 0, 2, accounts)
        start = time.perf_counter()
        for i in range(count):
            transfer(shard, i)
        writes = time.perf_counter() - start
        start = time.perf_counter()
        shard.snapshot()
        snapshot = time.perf_counter() - start
        for i in range(count, count + args.tail):
            transfer(shard, i)
        start = time.perf_counter()
        restored = Shard(path, 0, 2, accounts)
        restart = time.perf_counter() - start
        assert restored.audit()["matches"]
        return {
            "events": count,
            "tail_events": args.tail,
            "write_total_s": round(writes, 3),
            "snapshot_ms": round(snapshot * 1000, 1),
            "restart_ms": round(restart * 1000, 1),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


result = {
    "python": platform.python_version(),
    "platform": platform.platform(),
    "runs": [measure(count) for count in args.events],
}
print(json.dumps(result, indent=2))
if args.output:
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
