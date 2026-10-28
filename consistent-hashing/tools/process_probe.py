"""Verify deterministic routing in four synchronized, independent processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from consistent_hashing import (
    ConsistentHashRing,
    iter_migrations,
    md5_hash,
    sha1_hash,
    sha256_hash,
)

_barrier = None


def initialize(barrier):
    global _barrier
    _barrier = barrier


def digest(view, keys):
    assignments = [(key, view.get_nodes(key, 3)) for key in keys]
    return hashlib.sha256(
        json.dumps(assignments, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def worker(index):
    nodes = [f"server-{number}" for number in range(10)]
    if index % 2:
        nodes.reverse()
    keys = [f"key-{number}" for number in range(2000)] + ["", "用户:42"]
    _barrier.wait(timeout=20)
    report = {}
    for function in (sha1_hash, md5_hash, sha256_hash):
        ring = ConsistentHashRing(nodes, 200, function)
        initial = ring.snapshot()
        ring.set_virtual_nodes("server-0", 400)
        weighted = ring.snapshot()
        ring.add_node("server-10")
        expanded = ring.snapshot()
        add_plan = list(iter_migrations(weighted, expanded, iter(keys)))
        assert all(item.new_owner == "server-10" for item in add_plan)
        ring.remove_node("server-3")
        reduced = ring.snapshot()
        remove_plan = list(iter_migrations(expanded, reduced, iter(keys)))
        assert all(item.previous_owner == "server-3" for item in remove_plan)
        report[function.__name__] = {
            "initial": digest(initial, keys),
            "weighted": digest(weighted, keys),
            "expanded": digest(expanded, keys),
            "reduced": digest(reduced, keys),
            "moved_add": len(add_plan),
            "moved_remove": len(remove_plan),
        }
    return {"pid": os.getpid(), "routes": report}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    context = multiprocessing.get_context("spawn")
    start = time.perf_counter()
    with ProcessPoolExecutor(
        4, mp_context=context, initializer=initialize, initargs=(context.Barrier(4),)
    ) as pool:
        replies = list(pool.map(worker, range(4)))
    assert len({reply["pid"] for reply in replies}) == 4
    assert all(reply["routes"] == replies[0]["routes"] for reply in replies)
    report = {
        "processes": 4,
        "keys_per_phase": 2002,
        "phases": 4,
        "membership_order_varies": True,
        "identical_routes": True,
        "elapsed_seconds": time.perf_counter() - start,
        "routes": replies[0]["routes"],
        "scope": "Independent local processes; placement only, no network data transfer or distributed membership.",
    }
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
