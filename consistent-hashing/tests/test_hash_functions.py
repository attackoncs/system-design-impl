"""Unit tests for hash function implementations."""

import pytest

from consistent_hashing.hash_functions import md5_hash, sha1_hash, sha256_hash


HASH_FUNCTIONS = [sha1_hash, md5_hash, sha256_hash]
HASH_FUNCTION_IDS = ["sha1_hash", "md5_hash", "sha256_hash"]


class TestDeterministicOutput:
    """Same input always produces the same output."""

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_same_input_same_output(self, hash_fn):
        key = "test-key"
        assert hash_fn(key) == hash_fn(key)

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_deterministic_across_multiple_calls(self, hash_fn):
        key = "server-node-42"
        results = [hash_fn(key) for _ in range(100)]
        assert all(r == results[0] for r in results)

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_deterministic_with_empty_string(self, hash_fn):
        assert hash_fn("") == hash_fn("")


class TestDifferentInputsDifferentHashes:
    """Different inputs produce different hash values."""

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_different_keys_produce_different_hashes(self, hash_fn):
        assert hash_fn("key-a") != hash_fn("key-b")

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_similar_keys_produce_different_hashes(self, hash_fn):
        assert hash_fn("node#0") != hash_fn("node#1")

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_many_unique_inputs_produce_unique_hashes(self, hash_fn):
        keys = [f"key-{i}" for i in range(1000)]
        hashes = [hash_fn(k) for k in keys]
        assert len(set(hashes)) == len(keys)


class TestOutputIsPositiveInteger:
    """Output is a non-negative integer."""

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_output_is_int(self, hash_fn):
        result = hash_fn("some-key")
        assert isinstance(result, int)

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_output_is_non_negative(self, hash_fn):
        result = hash_fn("some-key")
        assert result >= 0

    @pytest.mark.parametrize("hash_fn", HASH_FUNCTIONS, ids=HASH_FUNCTION_IDS)
    def test_output_is_positive_integer_for_various_inputs(self, hash_fn):
        inputs = ["", "a", "hello world", "server-1#99", "?"]
        for key in inputs:
            result = hash_fn(key)
            assert isinstance(result, int), f"Expected int for input {key!r}, got {type(result)}"
            assert result >= 0, f"Expected >= 0 for input {key!r}, got {result}"
