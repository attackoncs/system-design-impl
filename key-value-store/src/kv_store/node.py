"""Node orchestrator for the distributed key-value store.

The KVNode class is the top-level entry point that initializes and coordinates
all sub-components: storage engine, gossip protocol, cluster membership, hinted
handoff, merkle tree, anti-entropy, gRPC client/server, quorum manager, and
request coordinator.
"""

from __future__ import annotations

import asyncio
import logging
import dataclasses
from pathlib import Path

from kv_store.replication.records import ReplicaRecords
from kv_store.cluster.placement import ReplicaPlacement
from kv_store.cluster.repair import HintJournal, ReplicaRepair
from typing import Optional

from consistent_hashing import ConsistentHashRing

from kv_store import config as kv_store_config
from kv_store.cluster import gossip as cluster_gossip
from kv_store.cluster import hinted_handoff as cluster_hinted_handoff
from kv_store.cluster import membership as cluster_membership
from kv_store.cluster import merkle_tree as cluster_merkle_tree
from kv_store.network import grpc_client as network_grpc_client
from kv_store.network import grpc_server as network_grpc_server
from kv_store.replication import coordinator as replication_coordinator
from kv_store.replication import quorum as replication_quorum
from kv_store.replication import vector_clock as replication_vector_clock
from kv_store.storage import engine as storage_engine

logger = logging.getLogger(__name__)


class KVNode:
    """Top-level node orchestrator for the distributed key-value store.

    Initializes all sub-components with proper dependency injection and
    manages the node lifecycle (start/stop). Client operations (put/get/delete)
    are delegated to the RequestCoordinator.

    Args:
        config: Node configuration combining all sub-configs.
    """

    def __init__(self, config: kv_store_config.NodeConfig) -> None:
        self._config = config
        self._running = False
        self._seed_task = None

        # Storage layer
        self._storage_engine = storage_engine.StorageEngine(
            dataclasses.replace(config.storage, max_value_size_bytes=1024 * 1024)
        )

        # Consistent hash ring (from consistent-hashing library)
        self._hash_ring = ConsistentHashRing(
            hash_function=None,
            num_virtual_nodes=config.virtual_nodes,
        )

        self._placement = ReplicaPlacement(self._hash_ring, config.node_data_centers)

        # Network layer
        self._grpc_client = network_grpc_client.GRPCClient(
            timeout=5.0,
            max_message_size=config.network.max_message_size_bytes,
        )

        # Cluster layer - gossip protocol
        self._gossip = cluster_gossip.GossipProtocol(
            node_id=config.node_id,
            address=f"{config.network.host}:{config.network.port}",
            gossip_interval=config.cluster.gossip_interval_seconds,
            gossip_fanout=config.cluster.gossip_fanout,
            failure_timeout=config.cluster.failure_timeout_seconds,
            gossip_func=self._exchange_gossip,
            on_membership_change=self._sync_membership,
        )

        # Cluster layer - membership
        self._membership = cluster_membership.ClusterMembership(
            node_id=config.node_id,
            address=f"{config.network.host}:{config.network.port}",
            gossip=self._gossip,
            hash_ring=self._hash_ring,
            virtual_nodes=config.node_virtual_nodes.get(
                config.node_id, config.virtual_nodes
            ),
        )

        self._merkle_tree = cluster_merkle_tree.MerkleTree(
            bucket_count=config.cluster.merkle_tree_buckets
        )
        self._records = ReplicaRecords(self._storage_engine, self._merkle_tree)
        self._hints = HintJournal(
            Path(config.storage.data_dir) / "hints.sqlite",
            self._membership,
            self._grpc_client,
        )
        self._repair = ReplicaRepair(
            config.node_id,
            self._records,
            self._membership,
            self._placement,
            config.replication.n_replicas,
            self._grpc_client,
        )
        self._hinted_handoff = cluster_hinted_handoff.HintedHandoffManager(
            node_id=config.node_id,
            handoff_interval=config.cluster.hinted_handoff_interval_seconds,
            is_node_alive_func=self._membership.is_node_alive,
            delivery_round=self._hints.deliver,
        )
        self._anti_entropy = cluster_merkle_tree.AntiEntropyManager(
            node_id=config.node_id,
            merkle_tree=self._merkle_tree,
            sync_interval=config.cluster.anti_entropy_interval_seconds,
            get_peers_func=lambda: [
                m.node_id
                for m in self._membership.get_alive_members()
                if m.node_id != config.node_id
            ],
            peer_sync_func=self._repair.sync,
        )

        # Replication layer - quorum manager
        self._quorum_manager = replication_quorum.QuorumManager(config.replication)

        # Replication layer - request coordinator
        self._coordinator = replication_coordinator.RequestCoordinator(
            node_id=config.node_id,
            hash_ring=self._placement,
            quorum_manager=self._quorum_manager,
            grpc_client=self._grpc_client,
            storage_engine=self._storage_engine,
            membership=self._membership,
            hinted_handoff=self._hinted_handoff,
            replica_records=self._records,
            hint_journal=self._hints,
            max_clock_entries=config.replication.vector_clock_max_entries,
        )

        # Network layer - gRPC server
        self._grpc_server = network_grpc_server.GRPCServer(
            coordinator=self._coordinator,
            storage_engine=self._storage_engine,
            gossip_protocol=self._gossip,
            merkle_tree=self._merkle_tree,
            host=config.network.host,
            port=config.network.port,
            max_message_size=config.network.max_message_size_bytes,
            replica_records=self._records,
        )

    @property
    def config(self) -> kv_store_config.NodeConfig:
        """The node configuration."""
        return self._config

    @property
    def node_id(self) -> str:
        """This node's identifier."""
        return self._config.node_id

    @property
    def is_running(self) -> bool:
        """Whether the node is currently running."""
        return self._running

    @property
    def storage_engine(self) -> storage_engine.StorageEngine:
        """The storage engine instance."""
        return self._storage_engine

    @property
    def membership(self) -> cluster_membership.ClusterMembership:
        """The cluster membership instance."""
        return self._membership

    @property
    def hash_ring(self) -> ConsistentHashRing:
        """The consistent hash ring instance."""
        return self._hash_ring

    async def start(self) -> None:
        """Start the node and all sub-components.

        Start order:
        1. Storage engine (replay WAL, load SSTables)
        2. gRPC server (begin accepting connections)
        3. Join cluster (add to ring, start gossip)
        4. Hinted handoff (background delivery)
        5. Anti-entropy (background sync)

        Raises:
            RuntimeError: If the node is already running.
            Exception: If any component fails to start (partial cleanup
                attempted).
        """
        if self._running:
            raise RuntimeError("Node is already running")

        logger.info("Starting KVNode %s", self._config.node_id)

        try:
            self._coordinator.reopen()
            # 1. Start storage engine
            await self._storage_engine.start()
            await self._records.rebuild()
            logger.info("Storage engine started")

            # 2. Start gRPC server
            await self._grpc_server.start()
            logger.info(
                "gRPC server started on %s:%d",
                self._config.network.host,
                self._config.network.port,
            )

            # 3. Join cluster
            # Resolve seed addresses to real node identities, never synthetic IDs.
            self._gossip.advertise(
                f"{self._config.network.host}:{self._grpc_server.port}"
            )
            await self._membership.join_cluster(None)
            await self._contact_seeds()
            logger.info("Joined cluster")

            # 4. Start hinted handoff
            await self._hinted_handoff.start()
            logger.info("Hinted handoff started")

            # 5. Start anti-entropy
            await self._anti_entropy.start()
            logger.info("Anti-entropy started")

            self._running = True
            if self._config.cluster.seed_nodes:
                self._seed_task = asyncio.create_task(self._seed_loop())
            logger.info("KVNode %s is ready", self._config.node_id)

        except BaseException as e:
            logger.error("Failed to start node: %s", e)
            # Attempt partial cleanup
            await self._cleanup_on_failure()
            raise

    async def stop(self) -> None:
        """Stop the node and all sub-components.

        Stop order:
        1. Anti-entropy (stop background sync)
        2. Hinted handoff (stop background delivery)
        3. Leave cluster (stop gossip, remove from ring)
        4. gRPC server (stop accepting connections)
        5. Storage engine (flush and close)

        Raises:
            RuntimeError: If the node is not running.
        """
        if not self._running:
            raise RuntimeError("Node is not running")

        logger.info("Stopping KVNode %s", self._config.node_id)

        errors = await self._close_components()
        self._running = False
        if errors:
            raise RuntimeError("Node cleanup failed") from errors[0]

    async def put(
        self,
        key: str,
        value: bytes,
        client_clock: Optional[replication_vector_clock.VectorClock] = None,
        consistency: Optional[replication_quorum.ConsistencyLevel] = None,
    ) -> replication_coordinator.PutResult:
        """Write a key-value pair.

        Delegates to RequestCoordinator.
                Args:
                    key: Key to write (max 256 bytes).
                    value: Value to write (max 10 KB).
                    client_clock: Client clock for read-modify-write.
                    consistency: Optional consistency level override.

                Returns:
                    PutResult with success status and updated vector clock.
        """
        return await self._coordinator.put(key, value, client_clock, consistency)

    async def get(
        self,
        key: str,
        consistency: Optional[replication_quorum.ConsistencyLevel] = None,
    ) -> replication_coordinator.GetResult:
        """Read a key.

        Delegates to RequestCoordinator.
                Args:
                    key: Key to read.
                    consistency: Optional consistency level override.

                Returns:
                    GetResult with value and conflict information.
        """
        return await self._coordinator.get(key, consistency)

    async def delete(
        self,
        key: str,
        client_clock: Optional[replication_vector_clock.VectorClock] = None,
        consistency: Optional[replication_quorum.ConsistencyLevel] = None,
    ) -> replication_coordinator.DeleteResult:
        """Delete a key.

        Delegates to RequestCoordinator.
                Args:
                    key: Key to delete.
                    client_clock: Vector clock from client.
                    consistency: Optional consistency level override.

                Returns:
                    DeleteResult with success status.
        """
        return await self._coordinator.delete(key, client_clock, consistency)

    def _sync_membership(self):
        # Failure suspicion does not delete an owner from the preference list:
        # sloppy quorum must remember where a recovered replica belongs.
        for member in self._gossip.members.values():
            if member.node_id not in self._hash_ring.nodes:
                self._hash_ring.add_node(
                    member.node_id,
                    self._config.node_virtual_nodes.get(
                        member.node_id, self._config.virtual_nodes
                    ),
                )

    def _merge_remote(self, rows):
        members = [
            cluster_gossip.MemberInfo(
                row["node_id"],
                row["address"],
                row["heartbeat_counter"],
                cluster_gossip.NodeStatus(row["status"]),
            )
            for row in rows
        ]
        self._gossip.merge_membership(members)
        self._sync_membership()

    async def _exchange_gossip(self, peer, members):
        address = self._membership.get_node_address(peer)
        rows = await self._grpc_client.gossip_exchange(address, members)
        self._merge_remote(rows)

    async def _contact_seeds(self):
        for address in self._config.cluster.seed_nodes:
            try:
                remote = await self._grpc_client.gossip_exchange(
                    address, list(self._gossip.members.values())
                )
                self._merge_remote(remote)
            except Exception:
                logger.debug("Seed unavailable: %s", address, exc_info=True)

    async def _seed_loop(self):
        while True:
            await asyncio.sleep(max(1.0, self._config.cluster.gossip_interval_seconds))
            await self._contact_seeds()

    async def _close_components(self):
        errors = []
        if self._seed_task is not None:
            self._seed_task.cancel()
            await asyncio.gather(self._seed_task, return_exceptions=True)
            self._seed_task = None
        operations = [
            self._anti_entropy.stop,
            self._hinted_handoff.stop,
            self._membership.leave_cluster,
            self._grpc_server.stop,
            self._coordinator.close,
            self._grpc_client.close,
            self._storage_engine.stop,
        ]
        for operation in operations:
            try:
                await operation()
            except Exception as error:
                errors.append(error)
                logger.exception("Component cleanup failed")
        return errors

    async def _cleanup_on_failure(self):
        await self._close_components()
