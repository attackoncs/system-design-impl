"""Node orchestrator for the distributed key-value store.

The KVNode class is the top-level entry point that initializes and coordinates
all sub-components: storage engine, gossip protocol, cluster membership, hinted
handoff, merkle tree, anti-entropy, gRPC client/server, quorum manager, and
request coordinator.
"""

from __future__ import annotations

import logging
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

        # Storage layer
        self._storage_engine = storage_engine.StorageEngine(config.storage)

        # Consistent hash ring (from consistent-hashing library)
        self._hash_ring = ConsistentHashRing(
            hash_function=None,
            num_virtual_nodes=config.virtual_nodes,
        )

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
        )

        # Cluster layer - membership
        self._membership = cluster_membership.ClusterMembership(
            node_id=config.node_id,
            address=f"{config.network.host}:{config.network.port}",
            gossip=self._gossip,
            hash_ring=self._hash_ring,
            virtual_nodes=config.virtual_nodes,
        )

        # Cluster layer - hinted handoff
        self._hinted_handoff = cluster_hinted_handoff.HintedHandoffManager(
            node_id=config.node_id,
            handoff_interval=config.cluster.hinted_handoff_interval_seconds,
            is_node_alive_func=self._membership.is_node_alive,
        )

        # Cluster layer - merkle tree and anti-entropy
        self._merkle_tree = cluster_merkle_tree.MerkleTree(
            bucket_count=config.cluster.merkle_tree_buckets,
        )

        self._anti_entropy = cluster_merkle_tree.AntiEntropyManager(
            node_id=config.node_id,
            merkle_tree=self._merkle_tree,
            sync_interval=config.cluster.anti_entropy_interval_seconds,
        )

        # Replication layer - quorum manager
        self._quorum_manager = replication_quorum.QuorumManager(config.replication)

        # Replication layer - request coordinator
        self._coordinator = replication_coordinator.RequestCoordinator(
            node_id=config.node_id,
            hash_ring=self._hash_ring,
            quorum_manager=self._quorum_manager,
            grpc_client=self._grpc_client,
            storage_engine=self._storage_engine,
            membership=self._membership,
            hinted_handoff=self._hinted_handoff,
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
            # 1. Start storage engine
            await self._storage_engine.start()
            logger.info("Storage engine started")

            # 2. Start gRPC server
            await self._grpc_server.start()
            logger.info(
                "gRPC server started on %s:%d",
                self._config.network.host,
                self._config.network.port,
            )

            # 3. Join cluster
            seed_nodes = [
                (f"seed-{i}", addr)
                for i, addr in enumerate(self._config.cluster.seed_nodes)
            ]
            await self._membership.join_cluster(seed_nodes if seed_nodes else None)
            logger.info("Joined cluster")

            # 4. Start hinted handoff
            await self._hinted_handoff.start()
            logger.info("Hinted handoff started")

            # 5. Start anti-entropy
            await self._anti_entropy.start()
            logger.info("Anti-entropy started")

            self._running = True
            logger.info("KVNode %s is ready", self._config.node_id)

        except Exception as e:
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

        # 1. Stop anti-entropy
        await self._anti_entropy.stop()
        logger.info("Anti-entropy stopped")

        # 2. Stop hinted handoff
        await self._hinted_handoff.stop()
        logger.info("Hinted handoff stopped")

        # 3. Leave cluster
        await self._membership.leave_cluster()
        logger.info("Left cluster")

        # 4. Stop gRPC server
        await self._grpc_server.stop()
        logger.info("gRPC server stopped")

        # 5. Stop storage engine
        await self._storage_engine.stop()
        logger.info("Storage engine stopped")

        self._running = False
        logger.info("KVNode %s stopped", self._config.node_id)

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

    async def _cleanup_on_failure(self) -> None:
        """Attempt to clean up partially started components on startup
        failure.
        """
        try:
            await self._anti_entropy.stop()
        # Isolate this operation; record failures before continuing.
        except Exception:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "_cleanup_on_failure"
            )
            pass
        try:
            await self._hinted_handoff.stop()
        # Isolate this operation; record failures before continuing.
        except Exception:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "_cleanup_on_failure"
            )
            pass
        try:
            await self._membership.leave_cluster()
        # Isolate this operation; record failures before continuing.
        except Exception:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "_cleanup_on_failure"
            )
            pass
        try:
            await self._grpc_server.stop()
        # Isolate this operation; record failures before continuing.
        except Exception:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "_cleanup_on_failure"
            )
            pass
        try:
            await self._storage_engine.stop()
        # Isolate this operation; record failures before continuing.
        except Exception:  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).exception(
                "Failure isolated in %s", "_cleanup_on_failure"
            )
            pass
