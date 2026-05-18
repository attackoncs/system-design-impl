"""Cluster membership management.

Wraps the gossip protocol and consistent hash ring to provide a unified
interface for cluster membership operations. Handles node join/leave events
and keeps the hash ring synchronized with the current membership state.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable


from kv_store.cluster.gossip import GossipProtocol, MemberInfo, NodeStatus


@runtime_checkable
class HashRingProtocol(Protocol):
    """Protocol defining the hash ring interface.

    This allows ClusterMembership to work with any hash ring implementation
    that provides add_node, remove_node, and nodes properties.
    """

    def add_node(self, node: str, num_virtual_nodes: Optional[int] = None) -> list[int]:
        """Add a node to the hash ring."""
        ...

    def remove_node(self, node: str) -> list[int]:
        """Remove a node from the hash ring."""
        ...

    @property
    def nodes(self) -> list[str]:
        """List of all physical nodes on the ring."""
        ...


class ClusterMembership:
    """Manages cluster membership by coordinating gossip and the hash ring.

    Provides high-level operations for joining/leaving the cluster and
    querying membership state. Keeps the consistent hash ring in sync
    with the gossip protocol's view of alive nodes.

    Args:
        node_id: This node's identifier.
        address: This node's address (host:port).
        gossip: The gossip protocol instance.
        hash_ring: The consistent hash ring instance.
        virtual_nodes: Number of virtual nodes per physical node.
    """

    def __init__(
        self,
        node_id: str,
        address: str,
        gossip: GossipProtocol,
        hash_ring: HashRingProtocol,
        virtual_nodes: int = 150,
    ):
        self._node_id = node_id
        self._address = address
        self._gossip = gossip
        self._hash_ring = hash_ring
        self._virtual_nodes = virtual_nodes
        self._joined = False

    @property
    def node_id(self) -> str:
        """This node's identifier."""
        return self._node_id

    @property
    def is_joined(self) -> bool:
        """Whether this node has joined the cluster."""
        return self._joined

    async def join_cluster(self, seed_nodes: Optional[list[tuple[str, str]]] = None) -> None:
        """Join the cluster.

        Adds this node to the hash ring, contacts seed nodes via gossip,
        and starts the gossip protocol.

        Args:
            seed_nodes: List of (node_id, address) tuples for seed nodes.
        """
        if self._joined:
            return

        # Add self to hash ring
        if self._node_id not in self._hash_ring.nodes:
            self._hash_ring.add_node(self._node_id, self._virtual_nodes)

        # Add seed nodes to gossip
        if seed_nodes:
            for node_id, address in seed_nodes:
                self._gossip.add_seed_node(node_id, address)

        # Start gossip protocol
        await self._gossip.start()
        self._joined = True

    async def leave_cluster(self) -> None:
        """Leave the cluster.

        Stops the gossip protocol and removes this node from the hash ring.
        """
        if not self._joined:
            return

        # Stop gossip
        await self._gossip.stop()

        # Remove self from hash ring
        if self._node_id in self._hash_ring.nodes:
            self._hash_ring.remove_node(self._node_id)

        self._joined = False

    def is_node_alive(self, node_id: str) -> bool:
        """Check if a node is alive.

        Args:
            node_id: The node to check.

        Returns:
            True if the node is known and has ALIVE status.
        """
        status = self._gossip.get_node_status(node_id)
        return status == NodeStatus.ALIVE

    def get_node_address(self, node_id: str) -> Optional[str]:
        """Get the address of a node.

        Args:
            node_id: The node to query.

        Returns:
            The node's address string, or None if unknown.
        """
        members = self._gossip.members
        info = members.get(node_id)
        if info is None:
            return None
        return info.address

    def get_alive_members(self) -> list[MemberInfo]:
        """Get all alive cluster members.

        Returns:
            List of MemberInfo for all nodes with ALIVE status.
        """
        return self._gossip.get_alive_nodes()

    def on_node_joined(self, node_id: str, address: str) -> None:
        """Handle a new node joining the cluster.

        Adds the node to the hash ring and gossip membership.

        Args:
            node_id: The joining node's identifier.
            address: The joining node's address.
        """
        # Add to gossip
        self._gossip.add_seed_node(node_id, address)

        # Add to hash ring
        if node_id not in self._hash_ring.nodes:
            self._hash_ring.add_node(node_id, self._virtual_nodes)

    def on_node_failed(self, node_id: str) -> None:
        """Handle a node failure.

        Removes the node from the hash ring.

        Args:
            node_id: The failed node's identifier.
        """
        if node_id in self._hash_ring.nodes:
            self._hash_ring.remove_node(node_id)
