"""Owned loopback cluster for examples and measurements; no external services."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from kv_store import KVNode, NodeConfig
from kv_store.config import (
    ClusterConfig,
    NetworkConfig,
    ReplicationConfig,
    StorageConfig,
)


class ClusterLab:
    def __init__(
        self,
        size=3,
        *,
        buckets=1024,
        message_size=16 * 1024 * 1024,
        memtable_size=4 * 1024 * 1024,
    ):
        if type(size) is not int or not 1 <= size <= 8:
            raise ValueError("lab size must be in1..8")
        self.size, self.buckets, self.message_size, self.memtable_size = (
            size,
            buckets,
            message_size,
            memtable_size,
        )
        self.nodes = []
        self._temporary = None

    async def __aenter__(self):
        folder = Path(__file__).resolve().parents[1] / ".runtime"
        folder.mkdir(exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="chapter07-", dir=folder)
        self.root = Path(self._temporary.name)
        try:
            for index in range(self.size):
                data = self.root / str(index)
                n = min(self.size, 3)
                config = NodeConfig(
                    node_id=f"n{index}",
                    network=NetworkConfig("127.0.0.1", 0, self.message_size),
                    storage=StorageConfig(
                        str(data),
                        str(data / "wal"),
                        str(data / "sstables"),
                        memtable_size_bytes=self.memtable_size,
                    ),
                    replication=ReplicationConfig(n, min(2, n), min(2, n)),
                    cluster=ClusterConfig(
                        gossip_interval_seconds=0.05,
                        failure_timeout_seconds=10,
                        anti_entropy_interval_seconds=3600,
                        hinted_handoff_interval_seconds=3600,
                        merkle_tree_buckets=self.buckets,
                        seed_nodes=[]
                        if not self.nodes
                        else [f"127.0.0.1:{self.nodes[0]._grpc_server.port}"],
                    ),
                )
                node = KVNode(config)
                await node.start()
                self.nodes.append(node)

            async def ready():
                while not all(
                    len(node.hash_ring.nodes) == self.size for node in self.nodes
                ):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(ready(), 5)
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        errors = []
        for node in reversed(self.nodes):
            try:
                await node.stop()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise errors[0]
        self.nodes.clear()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    async def __aexit__(self, *args):
        await self.close()
