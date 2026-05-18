"""Bloom filter implementation for SSTable key lookups.

A space-efficient probabilistic data structure used to test whether a key
is a member of a set. False positives are possible, but false negatives are not.
This is used in the LSM-tree read path to avoid unnecessary SSTable disk reads.

Covers: FR-10.2, FR-10.4
"""

import hashlib
import math
import struct
from typing import List, Tuple


class BloomFilter:
    """Space-efficient probabilistic data structure for set membership testing.

    Used to avoid unnecessary SSTable disk reads. A negative result guarantees
    the key is not in the SSTable; a positive result means the key might be present.

    Uses double hashing to simulate multiple independent hash functions:
        hash_i(key) = (hash1(key) + i * hash2(key)) % m

    where hash1 is derived from MD5 and hash2 from SHA-256.

    Covers: FR-10.2, FR-10.4
    """

    # Serialization format version for forward compatibility
    _FORMAT_VERSION = 1

    def __init__(
        self,
        expected_items: int,
        false_positive_rate: float = 0.01,
    ):
        """Initialize a Bloom filter with optimal size and hash count.

        Args:
            expected_items: Expected number of items to insert.
            false_positive_rate: Target false positive probability (default 1%).

        Raises:
            ValueError: If expected_items < 1 or false_positive_rate not in (0, 1).

        The bit array size and number of hash functions are computed
        from these parameters using the optimal formulas:
            m = -n * ln(p) / (ln(2))^2
            k = (m / n) * ln(2)
        """
        if expected_items < 1:
            raise ValueError("expected_items must be at least 1")
        if not (0 < false_positive_rate < 1):
            raise ValueError("false_positive_rate must be between 0 and 1 (exclusive)")

        self._expected_items = expected_items
        self._false_positive_rate = false_positive_rate

        # Optimal bit array size: m = -n * ln(p) / (ln(2))^2
        ln2 = math.log(2)
        ln2_sq = ln2 * ln2
        m = -expected_items * math.log(false_positive_rate) / ln2_sq
        self._size_bits = max(1, int(math.ceil(m)))

        # Optimal number of hash functions: k = (m / n) * ln(2)
        k = (self._size_bits / expected_items) * ln2
        self._num_hash_functions = max(1, int(round(k)))

        # Bit array stored as a bytearray
        num_bytes = (self._size_bits + 7) // 8
        self._bit_array = bytearray(num_bytes)

    def _get_hash_values(self, key: str) -> Tuple[int, int]:
        """Compute the two base hash values for double hashing.

        Args:
            key: The key to hash.

        Returns:
            Tuple of (hash1, hash2) as positive integers.
        """
        key_bytes = key.encode("utf-8")

        # hash1 from MD5 (first 8 bytes as unsigned 64-bit int)
        md5_digest = hashlib.md5(key_bytes).digest()
        hash1 = struct.unpack("<Q", md5_digest[:8])[0]

        # hash2 from SHA-256 (first 8 bytes as unsigned 64-bit int)
        sha256_digest = hashlib.sha256(key_bytes).digest()
        hash2 = struct.unpack("<Q", sha256_digest[:8])[0]

        return hash1, hash2

    def _get_bit_positions(self, key: str) -> List[int]:
        """Compute all bit positions for a key using double hashing.

        Uses the formula: position_i = (hash1 + i * hash2) % m

        Args:
            key: The key to compute positions for.

        Returns:
            List of bit positions (one per hash function).
        """
        hash1, hash2 = self._get_hash_values(key)
        positions = []
        for i in range(self._num_hash_functions):
            pos = (hash1 + i * hash2) % self._size_bits
            positions.append(pos)
        return positions

    def _set_bit(self, position: int) -> None:
        """Set a bit at the given position in the bit array."""
        byte_index = position // 8
        bit_offset = position % 8
        self._bit_array[byte_index] |= (1 << bit_offset)

    def _get_bit(self, position: int) -> bool:
        """Get the value of a bit at the given position."""
        byte_index = position // 8
        bit_offset = position % 8
        return bool(self._bit_array[byte_index] & (1 << bit_offset))

    def add(self, key: str) -> None:
        """Add a key to the Bloom filter.

        Args:
            key: The key to add.
        """
        for position in self._get_bit_positions(key):
            self._set_bit(position)

    def might_contain(self, key: str) -> bool:
        """Check if a key might be in the set.

        Args:
            key: The key to check.

        Returns:
            False means definitely not present.
            True means possibly present (subject to false positive rate).
        """
        for position in self._get_bit_positions(key):
            if not self._get_bit(position):
                return False
        return True

    def serialize(self) -> bytes:
        """Serialize the Bloom filter to bytes for storage in SSTable footer.

        Format:
            [version: 1 byte]
            [expected_items: 4 bytes, unsigned int]
            [false_positive_rate: 8 bytes, double]
            [size_bits: 4 bytes, unsigned int]
            [num_hash_functions: 2 bytes, unsigned short]
            [bit_array_length: 4 bytes, unsigned int]
            [bit_array: variable bytes]

        Returns:
            Serialized bytes representation of the Bloom filter.
        """
        header = struct.pack(
            "<BIdIHI",
            self._FORMAT_VERSION,
            self._expected_items,
            self._false_positive_rate,
            self._size_bits,
            self._num_hash_functions,
            len(self._bit_array),
        )
        return header + bytes(self._bit_array)

    @classmethod
    def deserialize(cls, data: bytes) -> "BloomFilter":
        """Deserialize a Bloom filter from bytes.

        Args:
            data: Bytes previously produced by serialize().

        Returns:
            A reconstructed BloomFilter instance.

        Raises:
            ValueError: If the data is corrupted or has an unsupported version.
        """
        # Header format: version(1) + expected_items(4) + fp_rate(8) + size_bits(4) + num_hashes(2) + array_len(4) = 23 bytes
        header_format = "<BIdIHI"
        header_size = struct.calcsize(header_format)
        if len(data) < header_size:
            raise ValueError("Data too short to contain a valid Bloom filter header")

        version, expected_items, fp_rate, size_bits, num_hashes, array_len = struct.unpack(
            header_format, data[:header_size]
        )

        if version != cls._FORMAT_VERSION:
            raise ValueError(f"Unsupported Bloom filter format version: {version}")

        if len(data) < header_size + array_len:
            raise ValueError("Data too short to contain the full bit array")

        bit_array_data = data[header_size: header_size + array_len]

        # Reconstruct the filter without recomputing parameters
        bf = cls.__new__(cls)
        bf._expected_items = expected_items
        bf._false_positive_rate = fp_rate
        bf._size_bits = size_bits
        bf._num_hash_functions = num_hashes
        bf._bit_array = bytearray(bit_array_data)

        return bf

    @property
    def size_bits(self) -> int:
        """Size of the bit array."""
        return self._size_bits

    @property
    def num_hash_functions(self) -> int:
        """Number of hash functions used."""
        return self._num_hash_functions
