"""gRPC server for the distributed key-value store.

Implements the KVStoreService defined in kvstore.proto, handling:
- Client-facing CRUD operations (Put, Get, Delete) via RequestCoordinator
- Inter-node replication (Replicate) writing directly to local StorageEngine
- Gossip membership exchange (GossipExchange) via GossipProtocol
- Hinted handoff delivery (HintedHandoff) writing to local storage
- Anti-entropy Merkle tree sync (MerkleTreeSync) comparing trees and returning diffs

Uses grpc.aio for async server support.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import grpc
import grpc.aio

from kv_store.network import kvstore_pb2
from kv_store.network import kvstore_pb2_grpc
from kv_store.replication.vector_clock import VectorClock

logger = logging.getLogger(__name__)


def vector_clock_to_proto(clock: VectorClock) -> kvstore_pb2.VectorClockProto:
    """Convert a VectorClock to its protobuf representation.

    Args:
        clock: The VectorClock instance to convert.

    Returns:
        VectorClockProto message.
    """
    proto = kvstore_pb2.VectorClockProto()
    for node_id, counter in clock.to_dict().items():
        entry = kvstore_pb2.VectorClockEntry(node_id=node_id, counter=counter)
        proto.entries.append(entry)
    return proto


def proto_to_vector_clock(proto: kvstore_pb2.VectorClockProto) -> VectorClock:
    """Convert a VectorClockProto to a VectorClock instance.

    Args:
        proto: The protobuf VectorClockProto message.

    Returns:
        VectorClock instance.
    """
    entries = {entry.node_id: entry.counter for entry in proto.entries}
    return VectorClock.from_dict(entries)


class KVStoreServicer(kvstore_pb2_grpc.KVStoreServiceServicer):
    """gRPC service implementation for the KVStoreService.

    Delegates client operations to the RequestCoordinator and handles
    inter-node communication for replication, gossip, hinted handoff,
    and anti-entropy sync.
    """

    def __init__(
        self,
        coordinator,
        storage_engine,
        gossip_protocol,
        merkle_tree,
    ):
        """Initialize the servicer with component references.

        Args:
            coordinator: RequestCoordinator for handling client Put/Get/Delete.
            storage_engine: StorageEngine for direct local writes (Replicate, HintedHandoff).
            gossip_protocol: GossipProtocol for membership exchange.
            merkle_tree: MerkleTree for anti-entropy sync.
        """
        self._coordinator = coordinator
        self._storage_engine = storage_engine
        self._gossip_protocol = gossip_protocol
        self._merkle_tree = merkle_tree

    async def Put(self, request, context):
        """Handle a Put RPC - delegate to RequestCoordinator.

        Args:
            request: PutRequest with key, value, vector_clock, consistency_level.
            context: gRPC service context.

        Returns:
            PutResponse with success status and updated vector clock.
        """
        try:
            # Parse vector clock from request
            client_clock = None
            if request.vector_clock and request.vector_clock.entries:
                client_clock = proto_to_vector_clock(request.vector_clock)

            # Parse consistency level
            from kv_store.replication.quorum import ConsistencyLevel
            consistency = None
            if request.consistency_level:
                try:
                    consistency = ConsistencyLevel(request.consistency_level)
                except ValueError:
                    pass

            result = await self._coordinator.put(
                key=request.key,
                value=request.value,
                client_clock=client_clock,
                consistency=consistency,
            )

            return kvstore_pb2.PutResponse(
                success=result.success,
                vector_clock=vector_clock_to_proto(result.vector_clock),
            )
        except Exception as e:
            logger.error("Put RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.PutResponse(success=False)

    async def Get(self, request, context):
        """Handle a Get RPC - delegate to RequestCoordinator.

        Args:
            request: GetRequest with key and consistency_level.
            context: gRPC service context.

        Returns:
            GetResponse with value, vector clock, and conflict information.
        """
        try:
            from kv_store.replication.quorum import ConsistencyLevel
            consistency = None
            if request.consistency_level:
                try:
                    consistency = ConsistencyLevel(request.consistency_level)
                except ValueError:
                    pass

            result = await self._coordinator.get(
                key=request.key,
                consistency=consistency,
            )

            response = kvstore_pb2.GetResponse(
                found=result.found,
                has_conflict=result.has_conflict,
            )

            if result.found and not result.has_conflict:
                if result.value is not None:
                    response.value = result.value
                if result.vector_clock is not None:
                    response.vector_clock.CopyFrom(
                        vector_clock_to_proto(result.vector_clock)
                    )

            if result.has_conflict:
                for value, clock in result.conflicting_values:
                    cv = kvstore_pb2.ConflictingValue(
                        value=value,
                        vector_clock=vector_clock_to_proto(clock),
                    )
                    response.conflicting_values.append(cv)

            return response
        except Exception as e:
            logger.error("Get RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.GetResponse(found=False)

    async def Delete(self, request, context):
        """Handle a Delete RPC - delegate to RequestCoordinator.

        Args:
            request: DeleteRequest with key, vector_clock, consistency_level.
            context: gRPC service context.

        Returns:
            DeleteResponse with success status and updated vector clock.
        """
        try:
            client_clock = None
            if request.vector_clock and request.vector_clock.entries:
                client_clock = proto_to_vector_clock(request.vector_clock)

            from kv_store.replication.quorum import ConsistencyLevel
            consistency = None
            if request.consistency_level:
                try:
                    consistency = ConsistencyLevel(request.consistency_level)
                except ValueError:
                    pass

            result = await self._coordinator.delete(
                key=request.key,
                client_clock=client_clock,
                consistency=consistency,
            )

            return kvstore_pb2.DeleteResponse(
                success=result.success,
                vector_clock=vector_clock_to_proto(result.vector_clock),
            )
        except Exception as e:
            logger.error("Delete RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.DeleteResponse(success=False)

    async def Replicate(self, request, context):
        """Handle a Replicate RPC - write directly to local StorageEngine.

        Used for inter-node replication of writes.

        Args:
            request: ReplicateRequest with key, value, vector_clock, timestamp, is_tombstone.
            context: gRPC service context.

        Returns:
            ReplicateResponse with success status.
        """
        try:
            if request.is_tombstone:
                await self._storage_engine.delete(request.key, request.timestamp)
            else:
                await self._storage_engine.put(
                    request.key, request.value, request.timestamp
                )
            return kvstore_pb2.ReplicateResponse(success=True)
        except Exception as e:
            logger.error("Replicate RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.ReplicateResponse(success=False)

    async def GossipExchange(self, request, context):
        """Handle a GossipExchange RPC - merge membership via GossipProtocol.

        Receives remote membership list, merges it with local state,
        and returns the local membership list.

        Args:
            request: GossipMessage with list of MemberInfoProto.
            context: gRPC service context.

        Returns:
            GossipMessage with local membership list.
        """
        try:
            from kv_store.cluster.gossip import MemberInfo, NodeStatus

            # Convert proto members to MemberInfo
            remote_members = []
            for member_proto in request.members:
                status = NodeStatus.ALIVE
                try:
                    status = NodeStatus(member_proto.status)
                except ValueError:
                    pass

                remote_members.append(
                    MemberInfo(
                        node_id=member_proto.node_id,
                        address=member_proto.address,
                        heartbeat_counter=member_proto.heartbeat_counter,
                        status=status,
                    )
                )

            # Merge remote membership
            self._gossip_protocol.merge_membership(remote_members)

            # Return local membership
            response = kvstore_pb2.GossipMessage()
            for member in self._gossip_protocol.members.values():
                member_proto = kvstore_pb2.MemberInfoProto(
                    node_id=member.node_id,
                    address=member.address,
                    heartbeat_counter=member.heartbeat_counter,
                    status=member.status.value,
                )
                response.members.append(member_proto)

            return response
        except Exception as e:
            logger.error("GossipExchange RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.GossipMessage()

    async def HintedHandoff(self, request, context):
        """Handle a HintedHandoff RPC - write to local storage.

        Receives hinted data that was stored on behalf of this node
        while it was unavailable.

        Args:
            request: HintedHandoffRequest with key, value, vector_clock, timestamp, is_tombstone.
            context: gRPC service context.

        Returns:
            HintedHandoffResponse with success status.
        """
        try:
            if request.is_tombstone:
                await self._storage_engine.delete(request.key, request.timestamp)
            else:
                await self._storage_engine.put(
                    request.key, request.value, request.timestamp
                )
            return kvstore_pb2.HintedHandoffResponse(success=True)
        except Exception as e:
            logger.error("HintedHandoff RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.HintedHandoffResponse(success=False)

    async def MerkleTreeSync(self, request, context):
        """Handle a MerkleTreeSync RPC - compare trees and return diffs.

        Receives remote bucket hashes, compares with local Merkle tree,
        and returns differing buckets along with local key-value pairs
        for those buckets.

        Args:
            request: MerkleTreeSyncRequest with bucket_hashes map.
            context: gRPC service context.

        Returns:
            MerkleTreeSyncResponse with differing_buckets and key_value_pairs.
        """
        try:
            # Convert proto bucket hashes to dict
            remote_hashes = {
                int(k): v for k, v in request.bucket_hashes.items()
            }

            # Compare with local tree
            diffs = self._merkle_tree.compare(remote_hashes)

            response = kvstore_pb2.MerkleTreeSyncResponse()
            for diff in diffs:
                response.differing_buckets.append(diff.bucket_id)

            # For each differing bucket, get local keys and their values
            for diff in diffs:
                keys = self._merkle_tree.get_keys_in_bucket(diff.bucket_id)
                for key in keys:
                    # Read from local storage to get the actual value
                    result = await self._storage_engine.get(key)
                    if result is not None and result.found:
                        kv_pair = kvstore_pb2.KeyValuePair(
                            key=key,
                            value=result.value if result.value else b"",
                            timestamp=result.timestamp,
                            is_tombstone=result.is_tombstone,
                        )
                        response.key_value_pairs.append(kv_pair)

            return response
        except Exception as e:
            logger.error("MerkleTreeSync RPC failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return kvstore_pb2.MerkleTreeSyncResponse()


class GRPCServer:
    """Async gRPC server for the distributed key-value store.

    Wraps the KVStoreServicer and manages the gRPC server lifecycle.
    Configurable host, port, and max message size.
    """

    def __init__(
        self,
        coordinator,
        storage_engine,
        gossip_protocol,
        merkle_tree,
        host: str = "0.0.0.0",
        port: int = 50051,
        max_message_size: int = 16 * 1024 * 1024,
    ):
        """Initialize the gRPC server.

        Args:
            coordinator: RequestCoordinator for client operations.
            storage_engine: StorageEngine for local writes.
            gossip_protocol: GossipProtocol for membership exchange.
            merkle_tree: MerkleTree for anti-entropy sync.
            host: Host address to bind to.
            port: Port number to listen on.
            max_message_size: Maximum message size in bytes.
        """
        self._coordinator = coordinator
        self._storage_engine = storage_engine
        self._gossip_protocol = gossip_protocol
        self._merkle_tree = merkle_tree
        self._host = host
        self._port = port
        self._max_message_size = max_message_size
        self._server: Optional[grpc.aio.Server] = None

    @property
    def port(self) -> int:
        """The port the server is configured to listen on."""
        return self._port

    @property
    def host(self) -> str:
        """The host the server is configured to bind to."""
        return self._host

    async def start(self) -> None:
        """Start the gRPC server and begin accepting connections."""
        options = [
            ("grpc.max_send_message_length", self._max_message_size),
            ("grpc.max_receive_message_length", self._max_message_size),
        ]

        self._server = grpc.aio.server(options=options)

        servicer = KVStoreServicer(
            coordinator=self._coordinator,
            storage_engine=self._storage_engine,
            gossip_protocol=self._gossip_protocol,
            merkle_tree=self._merkle_tree,
        )
        kvstore_pb2_grpc.add_KVStoreServiceServicer_to_server(
            servicer, self._server
        )

        bind_address = f"{self._host}:{self._port}"
        self._server.add_insecure_port(bind_address)

        await self._server.start()
        logger.info("gRPC server started on %s", bind_address)

    async def stop(self, grace: float = 5.0) -> None:
        """Stop the gRPC server gracefully.

        Args:
            grace: Grace period in seconds for in-flight requests.
        """
        if self._server is not None:
            await self._server.stop(grace)
            self._server = None
            logger.info("gRPC server stopped")

    async def wait_for_termination(self) -> None:
        """Block until the server terminates."""
        if self._server is not None:
            await self._server.wait_for_termination()
