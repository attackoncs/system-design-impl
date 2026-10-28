"""Actual gRPC subtree comparison and data pages below a64KiB message limit."""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(1, str(ROOT / "tools"))

from lab import ClusterLab

from kv_store.replication.records import Version
from kv_store.replication.vector_clock import VectorClock


async def main():
    async with ClusterLab(size=2, buckets=1, message_size=64 * 1024) as lab:
        left, right = lab.nodes
        payload = b"x" * 8192
        for index in range(24):
            await left._records.merge(
                f"key-{index:03}",
                [Version(payload, VectorClock({"n0": index + 1}), index)],
            )
        await right._records.merge(
            "deleted", [Version(None, VectorClock({"n1": 1}), 1)]
        )
        first = await right._repair.sync("n0")
        second = await right._repair.sync("n0")
        assert first["pages"] >= 4 and first["pulled_records"] == 24
        assert second["compared_nodes"] == 1 and second["pages"] == 0
        assert left._merkle_tree.get_root_hash() == right._merkle_tree.get_root_hash()
        assert (await left._records.read("deleted"))[0].value is None
        return {
            "nodes": 2,
            "message_limit_bytes": 65536,
            "application_bytes": 24 * len(payload),
            "first_repair": first,
            "second_repair": second,
            "roots_equal": True,
            "tombstone_preserved": True,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2))
