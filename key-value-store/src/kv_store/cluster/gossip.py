"""Gossip protocol for cluster membership and failure detection.

Implements a SWIM-style gossip protocol where each node periodically
sends its membership table to a random subset of peers. Failure detection
uses heartbeat counters and configurable timeouts to mark nodes as
SUSPECTED and eventually DOWN.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable, Optional


class NodeStatus(Enum):
    """Status of a node in the cluster."""

    ALIVE = "alive"
    SUSPECTED = "suspected"
    DOWN = "down"


@dataclass
class MemberInfo:
    """Information about a cluster member."""

    node_id: str
    address: str
    heartbeat_counter: int = 0
    status: NodeStatus = NodeStatus.ALIVE
    last_updated: float = field(default_factory=time.time)


# Type alias for the gossip send function.
# It receives the target node_id and the membership list to send.
GossipFunc = Callable[[str, list[MemberInfo]], Awaitable[None]]


class GossipProtocol:
    """Gossip-based cluster membership protocol.

    Periodically sends membership information to random peers (fanout).
    Detects failures by tracking heartbeat counters and marking nodes
    as SUSPECTED after a timeout, and DOWN after 2x the timeout.

    Args:
        node_id: This node's identifier.
        address: This node's address (host:port).
        gossip_interval: Seconds between gossip rounds.
        gossip_fanout: Number of peers to gossip with each round.
        failure_timeout: Seconds before marking a node as SUSPECTED.
        gossip_func: Async callable to send gossip messages to peers.
    """

    def __init__(
        self,
        node_id: str,
        address: str,
        gossip_interval: float = 1.0,
        gossip_fanout: int = 3,
        failure_timeout: float = 5.0,
        gossip_func: Optional[GossipFunc] = None,
    ):
        self._node_id = node_id
        self._address = address
        self._gossip_interval = gossip_interval
        self._gossip_fanout = gossip_fanout
        self._failure_timeout = failure_timeout
        self._gossip_func = gossip_func

        # Membership table: node_id -> MemberInfo
        self._members: dict[str, MemberInfo] = {
            node_id: MemberInfo(
                node_id=node_id,
                address=address,
                heartbeat_counter=0,
                status=NodeStatus.ALIVE,
                last_updated=time.time(),
            )
        }

        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def node_id(self) -> str:
        """This node's identifier."""
        return self._node_id

    @property
    def members(self) -> dict[str, MemberInfo]:
        """Current membership table."""
        return dict(self._members)

    async def start(self) -> None:
        """Start the gossip background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._gossip_loop())

    async def stop(self) -> None:
        """Stop the gossip background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _gossip_loop(self) -> None:
        """Background loop that runs gossip rounds at the configured interval."""
        while self._running:
            try:
                await self._gossip_round()
            except asyncio.CancelledError:
                break
            except Exception:
                # Log and continue on unexpected errors
                pass
            await asyncio.sleep(self._gossip_interval)

    async def _gossip_round(self) -> None:
        """Execute a single gossip round.

        1. Increment own heartbeat counter
        2. Select random peers (up to fanout)
        3. Send membership list to selected peers
        4. Check for timed-out nodes
        """
        # Increment own heartbeat
        self_info = self._members[self._node_id]
        self_info.heartbeat_counter += 1
        self_info.last_updated = time.time()
        self_info.status = NodeStatus.ALIVE

        # Select random peers
        peers = self._select_peers()

        # Send membership to peers
        if self._gossip_func and peers:
            membership_list = list(self._members.values())
            for peer_id in peers:
                try:
                    await self._gossip_func(peer_id, membership_list)
                except Exception:
                    # Peer unreachable, will be detected by timeout
                    pass

        # Check for timed-out nodes
        self._check_timeouts()

    def _select_peers(self) -> list[str]:
        """Select random peers for gossip (up to fanout count).

        Only selects nodes that are not DOWN and not self.
        """
        candidates = [
            node_id
            for node_id, info in self._members.items()
            if node_id != self._node_id and info.status != NodeStatus.DOWN
        ]
        count = min(self._gossip_fanout, len(candidates))
        if count == 0:
            return []
        return random.sample(candidates, count)

    def _check_timeouts(self) -> None:
        """Check all members for heartbeat timeouts.

        - ALIVE -> SUSPECTED after failure_timeout
        - SUSPECTED -> DOWN after 2x failure_timeout
        """
        now = time.time()
        for node_id, info in self._members.items():
            if node_id == self._node_id:
                continue

            elapsed = now - info.last_updated

            if info.status == NodeStatus.ALIVE and elapsed > self._failure_timeout:
                info.status = NodeStatus.SUSPECTED
            elif info.status == NodeStatus.SUSPECTED and elapsed > 2 * self._failure_timeout:
                info.status = NodeStatus.DOWN

    def merge_membership(self, remote_members: list[MemberInfo]) -> None:
        """Merge remote membership information with local state.

        For each remote member, take the higher heartbeat counter.
        New nodes are added to the membership table.

        Args:
            remote_members: Membership list received from a peer.
        """
        now = time.time()
        for remote in remote_members:
            if remote.node_id == self._node_id:
                # Don't overwrite our own info
                continue

            local = self._members.get(remote.node_id)
            if local is None:
                # New node discovered
                self._members[remote.node_id] = MemberInfo(
                    node_id=remote.node_id,
                    address=remote.address,
                    heartbeat_counter=remote.heartbeat_counter,
                    status=NodeStatus.ALIVE,
                    last_updated=now,
                )
            elif remote.heartbeat_counter > local.heartbeat_counter:
                # Remote has newer info
                local.heartbeat_counter = remote.heartbeat_counter
                local.address = remote.address
                local.last_updated = now
                # If we had marked it suspected/down but it's still heartbeating,
                # bring it back to alive
                if local.status != NodeStatus.ALIVE:
                    local.status = NodeStatus.ALIVE

    def get_alive_nodes(self) -> list[MemberInfo]:
        """Get all nodes with ALIVE status.

        Returns:
            List of MemberInfo for alive nodes.
        """
        return [
            info
            for info in self._members.values()
            if info.status == NodeStatus.ALIVE
        ]

    def get_node_status(self, node_id: str) -> Optional[NodeStatus]:
        """Get the status of a specific node.

        Args:
            node_id: The node to query.

        Returns:
            NodeStatus if the node is known, None otherwise.
        """
        info = self._members.get(node_id)
        if info is None:
            return None
        return info.status

    def add_seed_node(self, node_id: str, address: str) -> None:
        """Add a seed node to the membership table.

        Used during cluster bootstrap to establish initial connectivity.

        Args:
            node_id: The seed node's identifier.
            address: The seed node's address (host:port).
        """
        if node_id not in self._members:
            self._members[node_id] = MemberInfo(
                node_id=node_id,
                address=address,
                heartbeat_counter=0,
                status=NodeStatus.ALIVE,
                last_updated=time.time(),
            )
