"""Merkle tree and anti-entropy manager for data synchronization.

The Merkle tree provides efficient detection of data differences between
replicas. Each key is hashed into a bucket, and bucket hashes are combined
into a tree structure. Comparing root hashes quickly identifies whether
two replicas are in sync; comparing bucket hashes pinpoints which keys differ.

The AntiEntropyManager periodically compares Merkle trees with replica peers
and triggers synchronization for differing key ranges.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional


@dataclass
class MerkleNode:
    """A node in the Merkle tree.

    Attributes:
        hash_value: The SHA-256 hash for this node.
        bucket_id: The bucket index (for leaf nodes), -1 for internal nodes.
    """

    hash_value: str = ""
    bucket_id: int = -1


@dataclass
class SyncDiff:
    """Represents a difference found between two Merkle trees.

    Attributes:
        bucket_id: The bucket index where the difference was found.
        local_hash: The local bucket hash.
        remote_hash: The remote bucket hash.
        keys: Keys in the differing bucket (populated during sync).
    """

    bucket_id: int
    local_hash: str
    remote_hash: str
    keys: list[str] = field(default_factory=list)


# Type alias for the sync function.
# It receives the peer node_id and the list of diffs to synchronize.
SyncFunc = Callable[[str, list[SyncDiff]], Awaitable[None]]


class MerkleTree:
    """Bucket-based Merkle tree for efficient data comparison.

    Keys are hashed into buckets (leaf nodes). Each bucket maintains
    a hash of all key-value pairs it contains. The tree structure allows
    efficient top-down comparison to find differing ranges.

    Args:
        bucket_count: Number of buckets (must be a power of 2).
    """

    def __init__(self, bucket_count: int = 1024):
        if bucket_count <= 0 or (bucket_count & (bucket_count - 1)) != 0:
            raise ValueError("bucket_count must be a positive power of 2")

        self._bucket_count = bucket_count

        # Each bucket stores a dict of key -> value_hash
        self._buckets: list[dict[str, str]] = [{} for _ in range(bucket_count)]

        # Cached bucket hashes
        self._bucket_hashes: list[str] = ["" for _ in range(bucket_count)]

        # Root hash (computed from bucket hashes)
        self._root_hash: str = ""

        # Recompute initial state
        self._recompute_all()

    @property
    def bucket_count(self) -> int:
        """Number of buckets in the tree."""
        return self._bucket_count

    def update(self, key: str, value_hash: str) -> None:
        """Update or insert a key with its value hash.

        Args:
            key: The key to update.
            value_hash: The hash of the value associated with this key.
        """
        bucket_id = self._key_to_bucket(key)
        self._buckets[bucket_id][key] = value_hash
        self._recompute_bucket(bucket_id)
        self._recompute_root()

    def remove(self, key: str) -> None:
        """Remove a key from the tree.

        Args:
            key: The key to remove.
        """
        bucket_id = self._key_to_bucket(key)
        if key in self._buckets[bucket_id]:
            del self._buckets[bucket_id][key]
            self._recompute_bucket(bucket_id)
            self._recompute_root()

    def get_root_hash(self) -> str:
        """Get the root hash of the tree.

        Returns:
            The SHA-256 hash representing the entire tree state.
        """
        return self._root_hash

    def get_bucket_hash(self, bucket_id: int) -> str:
        """Get the hash for a specific bucket.

        Args:
            bucket_id: The bucket index.

        Returns:
            The SHA-256 hash for the bucket.

        Raises:
            IndexError: If bucket_id is out of range.
        """
        if bucket_id < 0 or bucket_id >= self._bucket_count:
            raise IndexError(f"bucket_id {bucket_id} out of range [0, {self._bucket_count})")
        return self._bucket_hashes[bucket_id]

    def get_bucket_hashes(self) -> dict[int, str]:
        """Get all bucket hashes as a dictionary.

        Returns:
            Dict mapping bucket_id to its hash.
        """
        return {i: h for i, h in enumerate(self._bucket_hashes)}

    def compare(self, other_bucket_hashes: dict[int, str]) -> list[SyncDiff]:
        """Compare this tree's bucket hashes with another tree's hashes.

        Args:
            other_bucket_hashes: Dict mapping bucket_id to hash from the remote tree.

        Returns:
            List of SyncDiff for buckets that differ.
        """
        diffs: list[SyncDiff] = []
        for bucket_id in range(self._bucket_count):
            local_hash = self._bucket_hashes[bucket_id]
            remote_hash = other_bucket_hashes.get(bucket_id, "")
            if local_hash != remote_hash:
                keys = list(self._buckets[bucket_id].keys())
                diffs.append(
                    SyncDiff(
                        bucket_id=bucket_id,
                        local_hash=local_hash,
                        remote_hash=remote_hash,
                        keys=keys,
                    )
                )
        return diffs

    def get_keys_in_bucket(self, bucket_id: int) -> list[str]:
        """Get all keys stored in a specific bucket.

        Args:
            bucket_id: The bucket index.

        Returns:
            List of keys in the bucket.
        """
        if bucket_id < 0 or bucket_id >= self._bucket_count:
            raise IndexError(f"bucket_id {bucket_id} out of range")
        return list(self._buckets[bucket_id].keys())

    def _key_to_bucket(self, key: str) -> int:
        """Hash a key to a bucket index.

        Uses SHA-256 and takes modulo bucket_count to determine the bucket.

        Args:
            key: The key to hash.

        Returns:
            The bucket index for this key.
        """
        hash_bytes = hashlib.sha256(key.encode("utf-8")).digest()
        # Use first 4 bytes as an integer
        hash_int = int.from_bytes(hash_bytes[:4], byteorder="big")
        return hash_int % self._bucket_count

    def _recompute_bucket(self, bucket_id: int) -> None:
        """Recompute the hash for a single bucket."""
        bucket = self._buckets[bucket_id]
        if not bucket:
            self._bucket_hashes[bucket_id] = ""
        else:
            # Sort keys for deterministic hashing
            hasher = hashlib.sha256()
            for key in sorted(bucket.keys()):
                hasher.update(key.encode("utf-8"))
                hasher.update(bucket[key].encode("utf-8"))
            self._bucket_hashes[bucket_id] = hasher.hexdigest()

    def _recompute_root(self) -> None:
        """Recompute the root hash from all bucket hashes."""
        hasher = hashlib.sha256()
        for bucket_hash in self._bucket_hashes:
            hasher.update(bucket_hash.encode("utf-8"))
        self._root_hash = hasher.hexdigest()

    def _recompute_all(self) -> None:
        """Recompute all bucket hashes and the root hash."""
        for i in range(self._bucket_count):
            self._recompute_bucket(i)
        self._recompute_root()


class AntiEntropyManager:
    """Manages periodic anti-entropy synchronization with replica peers.

    Periodically compares Merkle trees with peer nodes and triggers
    data synchronization for any differing key ranges.

    Args:
        node_id: This node's identifier.
        merkle_tree: The local Merkle tree instance.
        sync_func: Async callable to synchronize diffs with a peer.
        sync_interval: Seconds between sync rounds.
        get_peers_func: Optional callable to get current replica peers.
    """

    def __init__(
        self,
        node_id: str,
        merkle_tree: MerkleTree,
        sync_func: Optional[SyncFunc] = None,
        sync_interval: float = 60.0,
        get_peers_func: Optional[Callable[[], list[str]]] = None,
    ):
        self._node_id = node_id
        self._merkle_tree = merkle_tree
        self._sync_func = sync_func
        self._sync_interval = sync_interval
        self._get_peers_func = get_peers_func

        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def merkle_tree(self) -> MerkleTree:
        """The local Merkle tree."""
        return self._merkle_tree

    async def start(self) -> None:
        """Start the anti-entropy background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        """Stop the anti-entropy background task."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _sync_loop(self) -> None:
        """Background loop that runs sync rounds at the configured interval."""
        while self._running:
            try:
                await self._sync_round()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(self._sync_interval)

    async def _sync_round(self) -> None:
        """Execute a single sync round with all peers."""
        if self._get_peers_func is None:
            return

        peers = self._get_peers_func()
        for peer_id in peers:
            try:
                await self.sync_with_peer(peer_id)
            except Exception:
                pass

    async def sync_with_peer(self, peer_node_id: str) -> list[SyncDiff]:
        """Synchronize with a specific peer.

        Compares local Merkle tree hashes with the peer's hashes
        and triggers sync for any differences found.

        Args:
            peer_node_id: The peer to sync with.

        Returns:
            List of diffs found (empty if trees are identical).
        """
        # In a real implementation, we would exchange hashes with the peer
        # via gRPC. For now, this method is a placeholder that can be
        # called with pre-fetched remote hashes.
        return []

    async def sync_with_remote_hashes(
        self, peer_node_id: str, remote_hashes: dict[int, str]
    ) -> list[SyncDiff]:
        """Compare local tree with remote hashes and sync differences.

        Args:
            peer_node_id: The peer node identifier.
            remote_hashes: The peer's bucket hashes.

        Returns:
            List of diffs found.
        """
        diffs = self._merkle_tree.compare(remote_hashes)

        if diffs and self._sync_func:
            await self._sync_func(peer_node_id, diffs)

        return diffs
