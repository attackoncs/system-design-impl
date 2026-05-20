"""Unit tests for url_shortener.strategies module.

Tests cover:
- encode_base62 / decode_base62 with known values
- HashCollisionStrategy generates 7-char base-62 codes
- Collision resolution appends suffix and retries
- CollisionLimitExceededError raised after max retries
- Base62Strategy generates 7-char codes from sequential IDs
- AutoIncrementIDGenerator produces sequential values
- Custom IDGenerator works with Base62Strategy
"""

import re

import pytest

from url_shortener.exceptions import CollisionLimitExceededError
from url_shortener.models import RedirectType, URLMapping
from url_shortener.storage import InMemoryStorage
from url_shortener.strategies import (
    BASE62_CHARS,
    SHORT_CODE_LENGTH,
    AutoIncrementIDGenerator,
    Base62Strategy,
    HashCollisionStrategy,
    encode_base62,
    decode_base62,
)

# Regex for valid short codes: exactly 7 chars from [0-9, a-z, A-Z]
VALID_CODE_PATTERN = re.compile(r"^[0-9a-zA-Z]{7}$")


# --- encode_base62 / decode_base62 tests ---


class TestEncodeBase62:
    """Tests for encode_base62 with known values."""

    def test_encode_zero(self):
        assert encode_base62(0) == "0"

    def test_encode_one(self):
        assert encode_base62(1) == "1"

    def test_encode_61(self):
        # 61 is the last single-digit base-62 value -> 'Z'
        assert encode_base62(61) == "Z"

    def test_encode_62(self):
        # 62 in base-62 is "10"
        assert encode_base62(62) == "10"

    def test_encode_known_value(self):
        # 62^2 = 3844 -> "100"
        assert encode_base62(3844) == "100"

    def test_encode_large_number(self):
        # 62^6 = 56800235584 -> "1000000"
        assert encode_base62(56800235584) == "1000000"

    def test_encode_uses_correct_charset(self):
        # Verify all characters in output are from BASE62_CHARS
        for n in [0, 10, 36, 61, 100, 1000, 999999]:
            encoded = encode_base62(n)
            for ch in encoded:
                assert ch in BASE62_CHARS


class TestDecodeBase62:
    """Tests for decode_base62 with known values."""

    def test_decode_zero(self):
        assert decode_base62("0") == 0

    def test_decode_one(self):
        assert decode_base62("1") == 1

    def test_decode_Z(self):
        assert decode_base62("Z") == 61

    def test_decode_10(self):
        assert decode_base62("10") == 62

    def test_decode_100(self):
        assert decode_base62("100") == 3844

    def test_decode_invalid_character(self):
        with pytest.raises(ValueError, match="Invalid base-62 character"):
            decode_base62("abc!def")

    def test_decode_special_chars_invalid(self):
        with pytest.raises(ValueError):
            decode_base62("@#$")

    def test_encode_decode_roundtrip(self):
        """Encoding then decoding returns the original number."""
        for n in [0, 1, 42, 61, 62, 100, 999, 123456, 56800235584]:
            assert decode_base62(encode_base62(n)) == n


# --- HashCollisionStrategy tests ---


class TestHashCollisionStrategy:
    """Tests for HashCollisionStrategy."""

    def test_generates_7_char_code(self):
        """Generated code is exactly 7 characters from base-62 alphabet."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com/long-url", storage)
        assert VALID_CODE_PATTERN.match(code)

    def test_generates_valid_base62_chars(self):
        """All characters in generated code are from base-62 charset."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()
        code = strategy.generate("https://www.google.com/search?q=test", storage)
        assert len(code) == SHORT_CODE_LENGTH
        for ch in code:
            assert ch in BASE62_CHARS

    def test_same_url_same_code(self):
        """Same URL produces the same code (deterministic)."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()
        url = "https://example.com/page"
        code1 = strategy.generate(url, storage)
        code2 = strategy.generate(url, storage)
        assert code1 == code2

    def test_different_urls_may_differ(self):
        """Different URLs generally produce different codes."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()
        code1 = strategy.generate("https://example.com/a", storage)
        code2 = strategy.generate("https://example.com/b", storage)
        # Not guaranteed to differ (hash collisions possible), but very likely
        # We just verify both are valid
        assert VALID_CODE_PATTERN.match(code1)
        assert VALID_CODE_PATTERN.match(code2)

    def test_collision_resolution_retries(self):
        """When a collision occurs, strategy appends suffix and retries."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()

        url1 = "https://example.com/first"
        url2 = "https://example.com/second"

        # Generate code for url1 and store it
        code1 = strategy.generate(url1, storage)
        mapping1 = URLMapping(short_code=code1, long_url=url1)
        storage.save(mapping1)

        # Now generate for url2 - if it collides, it should resolve
        code2 = strategy.generate(url2, storage)
        assert VALID_CODE_PATTERN.match(code2)

        # If there was a collision, codes should differ
        # If no collision, they naturally differ
        if code1 == code2:
            # This shouldn't happen since url2 != url1
            pytest.fail("Same code generated for different URLs without resolution")

    def test_collision_resolution_with_forced_collision(self):
        """Force a collision by pre-populating storage with the expected code."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()

        url = "https://example.com/test"

        # First, compute what code this URL would get
        first_code = strategy.generate(url, storage)

        # Now store a DIFFERENT URL with that same code to force collision
        other_url = "https://other.com/different"
        mapping = URLMapping(short_code=first_code, long_url=other_url)
        storage.save(mapping)

        # Now generating for our URL should detect collision and retry
        resolved_code = strategy.generate(url, storage)
        assert VALID_CODE_PATTERN.match(resolved_code)
        # The resolved code should differ from the colliding one
        assert resolved_code != first_code

    def test_returns_existing_code_for_same_url(self):
        """If the code already maps to the same URL, return it without retry."""
        strategy = HashCollisionStrategy()
        storage = InMemoryStorage()

        url = "https://example.com/test"
        code = strategy.generate(url, storage)

        # Store the mapping
        mapping = URLMapping(short_code=code, long_url=url)
        storage.save(mapping)

        # Generating again for the same URL should return the same code
        code_again = strategy.generate(url, storage)
        assert code_again == code

    def test_collision_limit_exceeded_error(self):
        """CollisionLimitExceededError raised after max retries."""
        # Use max_retries=0 so it fails immediately on first collision
        strategy = HashCollisionStrategy(max_retries=0)
        storage = InMemoryStorage()

        url = "https://example.com/test"

        # Compute the code and store a different URL with it
        code = strategy.generate(url, storage)
        other_mapping = URLMapping(short_code=code, long_url="https://other.com/block")
        storage.save(other_mapping)

        # Now generating should fail since max_retries=0
        with pytest.raises(CollisionLimitExceededError):
            strategy.generate(url, storage)

    def test_collision_limit_exceeded_with_multiple_retries(self):
        """CollisionLimitExceededError raised after exhausting all retries."""
        max_retries = 2
        strategy = HashCollisionStrategy(max_retries=max_retries)
        storage = InMemoryStorage()

        url = "https://example.com/exhaust"

        # Pre-compute all codes that would be generated during retries
        # and block them all with different URLs
        url_to_hash = url
        for i in range(max_retries + 1):
            code = strategy._compute_code(url_to_hash)
            blocking_mapping = URLMapping(
                short_code=code,
                long_url=f"https://blocker.com/{i}",
            )
            storage.save(blocking_mapping)
            url_to_hash = url_to_hash + HashCollisionStrategy.COLLISION_SUFFIX

        # Now all retry slots are blocked
        with pytest.raises(CollisionLimitExceededError):
            strategy.generate(url, storage)


# --- Base62Strategy tests ---


class TestBase62Strategy:
    """Tests for Base62Strategy."""

    def test_generates_7_char_code(self):
        """Generated code is exactly 7 characters."""
        strategy = Base62Strategy()
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com", storage)
        assert len(code) == SHORT_CODE_LENGTH

    def test_generates_valid_base62_chars(self):
        """All characters in generated code are from base-62 charset."""
        strategy = Base62Strategy()
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com", storage)
        assert VALID_CODE_PATTERN.match(code)

    def test_sequential_codes_are_unique(self):
        """Sequential IDs produce unique codes."""
        strategy = Base62Strategy()
        storage = InMemoryStorage()
        codes = set()
        for i in range(100):
            code = strategy.generate(f"https://example.com/{i}", storage)
            codes.add(code)
        assert len(codes) == 100

    def test_codes_are_padded_to_7_chars(self):
        """Small IDs are left-padded with zeros to 7 characters."""
        strategy = Base62Strategy(id_generator=AutoIncrementIDGenerator(start=1))
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com", storage)
        # ID=1 encodes to "1", padded to "0000001"
        assert code == "0000001"

    def test_second_code_increments(self):
        """Second call produces the next sequential code."""
        strategy = Base62Strategy(id_generator=AutoIncrementIDGenerator(start=1))
        storage = InMemoryStorage()
        code1 = strategy.generate("https://example.com/a", storage)
        code2 = strategy.generate("https://example.com/b", storage)
        assert code1 == "0000001"
        assert code2 == "0000002"

    def test_custom_start_value(self):
        """Custom start value for ID generator is respected."""
        strategy = Base62Strategy(id_generator=AutoIncrementIDGenerator(start=62))
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com", storage)
        # 62 in base-62 is "10", padded to "0000010"
        assert code == "0000010"


# --- AutoIncrementIDGenerator tests ---


class TestAutoIncrementIDGenerator:
    """Tests for AutoIncrementIDGenerator."""

    def test_starts_at_one_by_default(self):
        gen = AutoIncrementIDGenerator()
        assert gen.next_id() == 1

    def test_produces_sequential_values(self):
        gen = AutoIncrementIDGenerator()
        assert gen.next_id() == 1
        assert gen.next_id() == 2
        assert gen.next_id() == 3

    def test_custom_start_value(self):
        gen = AutoIncrementIDGenerator(start=100)
        assert gen.next_id() == 100
        assert gen.next_id() == 101

    def test_many_sequential_values(self):
        gen = AutoIncrementIDGenerator(start=1)
        for expected in range(1, 51):
            assert gen.next_id() == expected


# --- Custom IDGenerator with Base62Strategy tests ---


class TestCustomIDGenerator:
    """Tests for custom IDGenerator implementations with Base62Strategy."""

    def test_custom_generator_is_used(self):
        """A custom IDGenerator conforming to the protocol works."""

        class FixedIDGenerator:
            """Always returns the same ID (for testing)."""

            def __init__(self, fixed_id: int):
                self._id = fixed_id

            def next_id(self) -> int:
                return self._id

        strategy = Base62Strategy(id_generator=FixedIDGenerator(42))
        storage = InMemoryStorage()
        code = strategy.generate("https://example.com", storage)
        # 42 in base-62 is "g" (index 42 in charset: 0-9=10, a-z=36, so 42-10=32 -> 'W'... 
        # Actually: 42 // 62 = 0, 42 % 62 = 42 -> BASE62_CHARS[42] = 'G'
        # Let's just verify it's 7 chars and valid
        assert VALID_CODE_PATTERN.match(code)
        # And it should be consistent
        code2 = strategy.generate("https://other.com", storage)
        assert code == code2  # Same ID always

    def test_custom_generator_sequence(self):
        """A custom generator producing a specific sequence works."""

        class ListIDGenerator:
            """Returns IDs from a predefined list."""

            def __init__(self, ids: list[int]):
                self._ids = iter(ids)

            def next_id(self) -> int:
                return next(self._ids)

        ids = [1, 10, 100, 1000]
        strategy = Base62Strategy(id_generator=ListIDGenerator(ids))
        storage = InMemoryStorage()

        codes = []
        for url_suffix in ["a", "b", "c", "d"]:
            code = strategy.generate(f"https://example.com/{url_suffix}", storage)
            codes.append(code)
            assert VALID_CODE_PATTERN.match(code)

        # All codes should be unique since IDs are unique
        assert len(set(codes)) == 4

        # Verify specific encodings
        assert codes[0] == "0000001"  # encode_base62(1) = "1" -> padded
        assert codes[1] == "000000a"  # encode_base62(10) = "a" -> padded
