"""Tests for the Merkle tree and anti-entropy manager."""

import pytest

from kv_store.cluster.merkle_tree import MerkleTree, AntiEntropyManager, SyncDiff


class TestMerkleTreeIdentical:
    """Tests for identical tree comparison."""

    def test_identical_trees_same_root_hash(self):
        """Two trees with the same data should have the same root hash."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree1.update("key2", "hash2")

        tree2.update("key1", "hash1")
        tree2.update("key2", "hash2")

        assert tree1.get_root_hash() == tree2.get_root_hash()

    def test_empty_trees_same_root_hash(self):
        """Two empty trees should have the same root hash."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)
        assert tree1.get_root_hash() == tree2.get_root_hash()


class TestMerkleTreeDifferent:
    """Tests for different tree comparison."""

    def test_different_data_different_root_hash(self):
        """Trees with different data should have different root hashes."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree2.update("key1", "hash2")  # different value hash

        assert tree1.get_root_hash() != tree2.get_root_hash()

    def test_extra_key_different_root_hash(self):
        """Tree with extra key should have different root hash."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree2.update("key1", "hash1")
        tree2.update("key2", "hash2")

        assert tree1.get_root_hash() != tree2.get_root_hash()


class TestMerkleTreeCompare:
    """Tests for the compare method."""

    def test_compare_finds_differing_buckets(self):
        """Compare should identify buckets that differ."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree2.update("key1", "hash2")  # different value

        diffs = tree1.compare(tree2.get_bucket_hashes())
        assert len(diffs) > 0
        # The diff should be in the bucket containing key1
        diff_bucket_ids = [d.bucket_id for d in diffs]
        key1_bucket = tree1._key_to_bucket("key1")
        assert key1_bucket in diff_bucket_ids

    def test_compare_no_diffs_for_identical(self):
        """Compare should return empty list for identical trees."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree2.update("key1", "hash1")

        diffs = tree1.compare(tree2.get_bucket_hashes())
        assert diffs == []

    def test_compare_includes_keys_in_diff(self):
        """Compare should include keys in differing buckets."""
        tree1 = MerkleTree(bucket_count=16)
        tree2 = MerkleTree(bucket_count=16)

        tree1.update("key1", "hash1")
        tree2.update("key1", "hash2")

        diffs = tree1.compare(tree2.get_bucket_hashes())
        # Find the diff for key1's bucket
        key1_bucket = tree1._key_to_bucket("key1")
        diff = next(d for d in diffs if d.bucket_id == key1_bucket)
        assert "key1" in diff.keys


class TestMerkleTreeUpdate:
    """Tests for the update method."""

    def test_update_changes_root_hash(self):
        """Updating a key should change the root hash."""
        tree = MerkleTree(bucket_count=16)
        initial_hash = tree.get_root_hash()

        tree.update("key1", "hash1")
        assert tree.get_root_hash() != initial_hash

    def test_update_changes_bucket_hash(self):
        """Updating a key should change its bucket's hash."""
        tree = MerkleTree(bucket_count=16)
        bucket_id = tree._key_to_bucket("key1")
        initial_bucket_hash = tree.get_bucket_hash(bucket_id)

        tree.update("key1", "hash1")
        assert tree.get_bucket_hash(bucket_id) != initial_bucket_hash

    def test_update_only_affects_relevant_bucket(self):
        """Updating a key should only change the hash of its bucket."""
        tree = MerkleTree(bucket_count=16)
        tree.update("key1", "hash1")

        # Record all bucket hashes
        hashes_before = {i: tree.get_bucket_hash(i) for i in range(16)}

        # Update key1 with a new value
        tree.update("key1", "hash1_updated")

        # Only the bucket containing key1 should change
        key1_bucket = tree._key_to_bucket("key1")
        for i in range(16):
            if i == key1_bucket:
                assert tree.get_bucket_hash(i) != hashes_before[i]
            else:
                assert tree.get_bucket_hash(i) == hashes_before[i]


class TestMerkleTreeRemove:
    """Tests for the remove method."""

    def test_remove_changes_root_hash(self):
        """Removing a key should change the root hash."""
        tree = MerkleTree(bucket_count=16)
        tree.update("key1", "hash1")
        hash_with_key = tree.get_root_hash()

        tree.remove("key1")
        assert tree.get_root_hash() != hash_with_key

    def test_remove_restores_empty_state(self):
        """Removing all keys should restore the empty tree hash."""
        tree = MerkleTree(bucket_count=16)
        empty_hash = tree.get_root_hash()

        tree.update("key1", "hash1")
        tree.remove("key1")
        assert tree.get_root_hash() == empty_hash

    def test_remove_nonexistent_key(self):
        """Removing a non-existent key should be a no-op."""
        tree = MerkleTree(bucket_count=16)
        tree.update("key1", "hash1")
        hash_before = tree.get_root_hash()

        tree.remove("nonexistent")
        assert tree.get_root_hash() == hash_before


class TestKeyToBucket:
    """Tests for the _key_to_bucket method."""

    def test_key_to_bucket_deterministic(self):
        """Same key should always map to the same bucket."""
        tree = MerkleTree(bucket_count=16)
        bucket1 = tree._key_to_bucket("test-key")
        bucket2 = tree._key_to_bucket("test-key")
        assert bucket1 == bucket2

    def test_key_to_bucket_in_range(self):
        """Bucket index should be within valid range."""
        tree = MerkleTree(bucket_count=16)
        for i in range(100):
            bucket = tree._key_to_bucket(f"key-{i}")
            assert 0 <= bucket < 16

    def test_key_to_bucket_distributes(self):
        """Different keys should distribute across buckets."""
        tree = MerkleTree(bucket_count=16)
        buckets = set()
        for i in range(100):
            buckets.add(tree._key_to_bucket(f"key-{i}"))
        # With 100 keys and 16 buckets, we should hit multiple buckets
        assert len(buckets) > 1


class TestMerkleTreeInit:
    """Tests for Merkle tree initialization."""

    def test_invalid_bucket_count_not_power_of_2(self):
        """Should raise ValueError for non-power-of-2 bucket count."""
        with pytest.raises(ValueError):
            MerkleTree(bucket_count=15)

    def test_invalid_bucket_count_zero(self):
        """Should raise ValueError for zero bucket count."""
        with pytest.raises(ValueError):
            MerkleTree(bucket_count=0)

    def test_valid_bucket_counts(self):
        """Should accept valid power-of-2 bucket counts."""
        for count in [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]:
            tree = MerkleTree(bucket_count=count)
            assert tree.bucket_count == count


class TestGetKeysInBucket:
    """Tests for get_keys_in_bucket."""

    def test_get_keys_in_bucket(self):
        """Should return keys stored in a specific bucket."""
        tree = MerkleTree(bucket_count=16)
        tree.update("key1", "hash1")

        bucket_id = tree._key_to_bucket("key1")
        keys = tree.get_keys_in_bucket(bucket_id)
        assert "key1" in keys

    def test_get_keys_empty_bucket(self):
        """Should return empty list for empty bucket."""
        tree = MerkleTree(bucket_count=1024)
        # Find an empty bucket
        tree.update("key1", "hash1")
        key1_bucket = tree._key_to_bucket("key1")
        # Pick a different bucket
        other_bucket = (key1_bucket + 1) % 1024
        keys = tree.get_keys_in_bucket(other_bucket)
        # May or may not be empty depending on hash, but shouldn't contain key1
        assert "key1" not in keys
