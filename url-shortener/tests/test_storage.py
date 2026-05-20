"""Unit tests for the storage layer.

Validates: Requirements 5.1, 5.2, 5.3, 5.4
"""

import pytest

from url_shortener.models import RedirectType, URLMapping
from url_shortener.storage import InMemoryStorage, StorageBackend


class TestInMemoryStorage:
    """Tests for InMemoryStorage implementation."""

    def setup_method(self) -> None:
        self.storage = InMemoryStorage()

    def test_save_and_get_by_short_code(self) -> None:
        """save() then get_by_short_code() returns the mapping."""
        mapping = URLMapping(
            short_code="abc1234",
            long_url="https://example.com/page",
        )
        self.storage.save(mapping)

        result = self.storage.get_by_short_code("abc1234")
        assert result is not None
        assert result.short_code == "abc1234"
        assert result.long_url == "https://example.com/page"

    def test_save_and_get_by_long_url(self) -> None:
        """save() then get_by_long_url() returns the mapping (bidirectional)."""
        mapping = URLMapping(
            short_code="xyz7890",
            long_url="https://example.com/another",
        )
        self.storage.save(mapping)

        result = self.storage.get_by_long_url("https://example.com/another")
        assert result is not None
        assert result.short_code == "xyz7890"
        assert result.long_url == "https://example.com/another"

    def test_exists_returns_true_for_saved_code(self) -> None:
        """exists() returns True for a saved short code."""
        mapping = URLMapping(
            short_code="exists1",
            long_url="https://example.com/exists",
        )
        self.storage.save(mapping)

        assert self.storage.exists("exists1") is True

    def test_exists_returns_false_for_absent_code(self) -> None:
        """exists() returns False for a code not in storage."""
        assert self.storage.exists("nocode1") is False

    def test_redirect_type_preserved(self) -> None:
        """RedirectType is preserved in stored mapping."""
        mapping_permanent = URLMapping(
            short_code="perm123",
            long_url="https://example.com/permanent",
            redirect_type=RedirectType.PERMANENT,
        )
        mapping_temporary = URLMapping(
            short_code="temp123",
            long_url="https://example.com/temporary",
            redirect_type=RedirectType.TEMPORARY,
        )
        self.storage.save(mapping_permanent)
        self.storage.save(mapping_temporary)

        result_perm = self.storage.get_by_short_code("perm123")
        result_temp = self.storage.get_by_short_code("temp123")

        assert result_perm is not None
        assert result_perm.redirect_type == RedirectType.PERMANENT
        assert result_perm.redirect_type == 301

        assert result_temp is not None
        assert result_temp.redirect_type == RedirectType.TEMPORARY
        assert result_temp.redirect_type == 302

    def test_overwrite_short_code_updates_both_dicts(self) -> None:
        """Overwriting a short code with a new mapping updates both dicts."""
        original = URLMapping(
            short_code="over123",
            long_url="https://example.com/original",
        )
        self.storage.save(original)

        replacement = URLMapping(
            short_code="over123",
            long_url="https://example.com/replacement",
        )
        self.storage.save(replacement)

        # Short code now points to the new URL
        result = self.storage.get_by_short_code("over123")
        assert result is not None
        assert result.long_url == "https://example.com/replacement"

        # New URL resolves to the mapping
        result_by_url = self.storage.get_by_long_url("https://example.com/replacement")
        assert result_by_url is not None
        assert result_by_url.short_code == "over123"

    def test_get_by_short_code_returns_none_for_nonexistent(self) -> None:
        """get_by_short_code returns None for non-existent code."""
        result = self.storage.get_by_short_code("missing")
        assert result is None

    def test_get_by_long_url_returns_none_for_nonexistent(self) -> None:
        """get_by_long_url returns None for non-existent URL."""
        result = self.storage.get_by_long_url("https://nowhere.example.com")
        assert result is None

    def test_implements_storage_backend_interface(self) -> None:
        """InMemoryStorage is a subclass of StorageBackend."""
        assert isinstance(self.storage, StorageBackend)
