"""Gossip protocol for cluster membership and failure detection.

Implements a SWIM-style gossip protocol where each node periodically sends its
membership table to a random subset of peers. Failure detection uses heartbeat
counters and configurable timeouts to mark nodes as SUSPECTED and eventually
DOWN.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import random
import time
from typing import Awaitable, Callable, Optional

from kv_store.async_work import complete


class NodeStatus(enum.Enum):
    """Status of a node in the cluster."""

    ALIVE = "alive"
    SUSPECTED = "suspected"
    DOWN = "down"


@dataclasses.dataclass
class MemberInfo:
    """Information about a cluster member."""

    node_id: str
    address: str
    heartbeat_counter: int = 0
    status: NodeStatus = NodeStatus.ALIVE
    last_updated: float = dataclasses.field(default_factory=time.time)


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
        on_membership_change=None,
    ):
        self._on_membership_change = on_membership_change
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
        self._down_probe_cursor = 0

    def advertise(self, address: str) -> None:
        """Publish the bound listening address (including an ephemeral port)."""
        self._address = address
        self._members[self._node_id] = dataclasses.replace(
            self._members[self._node_id], address=address
        )

    @property
    def node_id(self) -> str:
        """This node's identifier."""
        return self._node_id

    @property
    def members(self) -> dict[str, MemberInfo]:
        """Current membership table."""
        return {node: dataclasses.replace(info) for node, info in self._members.items()}

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
        """Background loop that runs gossip rounds at the configured
        interval.
        """
        while self._running:
            try:
                await self._gossip_round()
            except asyncio.CancelledError:
                break
            # Isolate this operation; record failures before continuing.
            except Exception:  # pylint: disable=broad-exception-caught
                # Log and continue on unexpected errors
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "_gossip_loop"
                )
                pass
            await asyncio.sleep(self._gossip_interval)

    async def _gossip_round(self) -> None:
        """Exchange a fixed membership view concurrently, then detect failures."""
        self_info = self._members[self._node_id]
        self._members[self._node_id] = dataclasses.replace(
            self_info,
            heartbeat_counter=self_info.heartbeat_counter + 1,
            last_updated=time.time(),
            status=NodeStatus.ALIVE,
        )
        peers = self._select_peers()
        # A healed partition must recover even when every remembered peer is DOWN
        # and no configured seed is reachable. Probe just one, rotating fairly.
        down = [
            node
            for node, info in self._members.items()
            if node != self._node_id and info.status is NodeStatus.DOWN
        ]
        if down:
            peers.append(down[self._down_probe_cursor % len(down)])
            self._down_probe_cursor += 1

        if self._gossip_func and peers:
            members = list(self.members.values())
            tasks = [
                asyncio.create_task(self._send_peer(peer, members)) for peer in peers
            ]
            group = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(group)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # Repeated stop/cancellation must not abandon transport cleanup.
                await complete(group)

        self._check_timeouts()

    async def _send_peer(self, peer: str, members: list[MemberInfo]) -> None:
        try:
            await self._gossip_func(
                peer, [dataclasses.replace(info) for info in members]
            )
        except Exception:
            logging.getLogger(__name__).exception("Gossip exchange failed: %s", peer)

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
                self._members[node_id] = dataclasses.replace(
                    info, status=NodeStatus.SUSPECTED
                )
            elif (
                info.status == NodeStatus.SUSPECTED
                and elapsed > 2 * self._failure_timeout
            ):
                self._members[node_id] = dataclasses.replace(
                    info, status=NodeStatus.DOWN
                )

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
                # Refute an older incarnation after a restart by advancing past
                # the counter peers remember. Node IDs must be unique among live nodes.
                own = self._members[self._node_id]
                if remote.heartbeat_counter >= own.heartbeat_counter:
                    self._members[self._node_id] = dataclasses.replace(
                        own, heartbeat_counter=remote.heartbeat_counter + 1
                    )
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
                # If we had marked it suspected/down but it's still
                # heartbeating,
                # bring it back to alive
                self._members[remote.node_id] = dataclasses.replace(
                    local,
                    heartbeat_counter=remote.heartbeat_counter,
                    address=remote.address,
                    last_updated=now,
                    status=NodeStatus.ALIVE,
                )

        if self._on_membership_change is not None:
            self._on_membership_change()

    def get_alive_nodes(self) -> list[MemberInfo]:
        """Get all nodes with ALIVE status.

        Returns:
            List of MemberInfo for alive nodes.
        """
        return [
            dataclasses.replace(info)
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
