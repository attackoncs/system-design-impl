"""Tests for the Bloom filter implementation.

Covers:
- No false negatives (added keys always found)
- False positive rate within expected bounds
- Serialization/deserialization roundtrip
- Optimal parameter calculation
- Empty filter returns False for all queries
"""

import math

import pytest

from kv_store.storage.bloom_filter import BloomFilter


class TestNoFalseNegatives:
    """Added keys must always be found by might_contain."""

    def test_single_key_found_after_add(self):
        bf = BloomFilter(expected_items=100)
        bf.add("hello")
        assert bf.might_contain("hello") is True

    def test_multiple_keys_all_found(self):
        bf = BloomFilter(expected_items=1000)
        keys = [f"key-{i}" for i in range(500)]
        for key in keys:
            bf.add(key)
        for key in keys:
            assert bf.might_contain(key) is True

    def test_keys_with_special_characters(self):
        bf = BloomFilter(expected_items=100)
        special_keys = ["", "key with spaces", "日本語", "emoji🎉", "a" * 256]
        for key in special_keys:
            bf.add(key)
        for key in special_keys:
            assert bf.might_contain(key) is True


class TestFalsePositiveRate:
    """False positive rate should be within expected bounds."""

    def test_false_positive_rate_within_bounds(self):
        n = 1000
        target_fp_rate = 0.01
        bf = BloomFilter(expected_items=n, false_positive_rate=target_fp_rate)

        # Add n items
        for i in range(n):
            bf.add(f"added-{i}")

        # Test with keys that were NOT added
        num_tests = 10000
        false_positives = 0
        for i in range(num_tests):
            if bf.might_contain(f"not-added-{i}"):
                false_positives += 1

        observed_rate = false_positives / num_tests
        # Allow up to 2x the target rate (statistical tolerance)
        assert observed_rate < target_fp_rate * 2, (
            f"False positive rate {observed_rate:.4f} exceeds 2x target {target_fp_rate}"
        )

    def test_higher_fp_rate_uses_fewer_bits(self):
        bf_low = BloomFilter(expected_items=1000, false_positive_rate=0.001)
        bf_high = BloomFilter(expected_items=1000, false_positive_rate=0.1)
        # Lower FP rate requires more bits
        assert bf_low.size_bits > bf_high.size_bits


class TestSerializationDeserialization:
    """Serialize and deserialize should produce an equivalent filter."""

    def test_roundtrip_preserves_membership(self):
        bf = BloomFilter(expected_items=100, false_positive_rate=0.01)
        keys = [f"key-{i}" for i in range(50)]
        for key in keys:
            bf.add(key)

        data = bf.serialize()
        restored = BloomFilter.deserialize(data)

        # All added keys must still be found
        for key in keys:
            assert restored.might_contain(key) is True

    def test_roundtrip_preserves_parameters(self):
        bf = BloomFilter(expected_items=500, false_positive_rate=0.05)
        data = bf.serialize()
        restored = BloomFilter.deserialize(data)

        assert restored.size_bits == bf.size_bits
        assert restored.num_hash_functions == bf.num_hash_functions

    def test_roundtrip_empty_filter(self):
        bf = BloomFilter(expected_items=100)
        data = bf.serialize()
        restored = BloomFilter.deserialize(data)

        # Empty filter should still return False for queries
        assert restored.might_contain("anything") is False

    def test_deserialize_invalid_data_raises(self):
        with pytest.raises(ValueError):
            BloomFilter.deserialize(b"short")

    def test_deserialize_wrong_version_raises(self):
        bf = BloomFilter(expected_items=10)
        data = bytearray(bf.serialize())
        data[0] = 99  # corrupt version byte
        with pytest.raises(ValueError, match="Unsupported Bloom filter format version"):
            BloomFilter.deserialize(bytes(data))


class TestOptimalParameterCalculation:
    """Verify that size_bits and num_hash_functions follow optimal formulas."""

    def test_size_bits_formula(self):
        n = 1000
        p = 0.01
        bf = BloomFilter(expected_items=n, false_positive_rate=p)

        # Expected: m = -n * ln(p) / (ln(2))^2
        ln2_sq = math.log(2) ** 2
        expected_m = -n * math.log(p) / ln2_sq
        expected_m = int(math.ceil(expected_m))

        assert bf.size_bits == expected_m

    def test_num_hash_functions_formula(self):
        n = 1000
        p = 0.01
        bf = BloomFilter(expected_items=n, false_positive_rate=p)

        # Expected: k = (m / n) * ln(2)
        expected_k = (bf.size_bits / n) * math.log(2)
        expected_k = int(round(expected_k))

        assert bf.num_hash_functions == expected_k

    def test_minimum_one_bit(self):
        # Even with extreme parameters, at least 1 bit
        bf = BloomFilter(expected_items=1, false_positive_rate=0.99)
        assert bf.size_bits >= 1

    def test_minimum_one_hash_function(self):
        bf = BloomFilter(expected_items=1, false_positive_rate=0.99)
        assert bf.num_hash_functions >= 1

    def test_invalid_expected_items_raises(self):
        with pytest.raises(ValueError):
            BloomFilter(expected_items=0)

    def test_invalid_fp_rate_raises(self):
        with pytest.raises(ValueError):
            BloomFilter(expected_items=100, false_positive_rate=0.0)
        with pytest.raises(ValueError):
            BloomFilter(expected_items=100, false_positive_rate=1.0)


class TestEmptyFilter:
    """An empty filter should return False for all queries."""

    def test_empty_filter_returns_false(self):
        bf = BloomFilter(expected_items=100)
        assert bf.might_contain("any-key") is False

    def test_empty_filter_returns_false_for_many_keys(self):
        bf = BloomFilter(expected_items=1000)
        for i in range(100):
            assert bf.might_contain(f"key-{i}") is False

    def test_empty_filter_returns_false_for_empty_string(self):
        bf = BloomFilter(expected_items=10)
        assert bf.might_contain("") is False
