"""Unit tests for the URLShortener orchestrator."""

import pytest

from url_shortener import (
    URLShortener,
    RedirectType,
    URLValidationError,
    ShortCodeNotFoundError,
    Base62Strategy,
    HashCollisionStrategy,
)


class TestShorten:
    """Tests for URLShortener.shorten()."""

    def test_shorten_returns_valid_short_url_with_domain_prefix(self):
        """Shorten returns a URL starting with the configured domain."""
        shortener = URLShortener(domain="http://short.url")
        result = shortener.shorten("https://example.com/page")

        assert result.startswith("http://short.url/")
        short_code = result.split("/")[-1]
        assert len(short_code) == 7
        assert all(c in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in short_code)

    def test_shorten_same_url_twice_returns_same_result(self):
        """Shortening the same URL twice returns the same short URL (idempotence)."""
        shortener = URLShortener()
        url = "https://example.com/idempotent"

        first = shortener.shorten(url)
        second = shortener.shorten(url)

        assert first == second

    def test_shorten_with_custom_redirect_type_stores_correctly(self):
        """Shortening with a custom redirect type preserves it on resolve."""
        shortener = URLShortener()
        url = "https://example.com/permanent"

        short_url = shortener.shorten(url, redirect_type=RedirectType.PERMANENT)
        short_code = short_url.split("/")[-1]
        _, redirect_type = shortener.resolve(short_code)

        assert redirect_type == RedirectType.PERMANENT

    def test_shorten_with_invalid_url_raises_url_validation_error(self):
        """Shortening an invalid URL raises URLValidationError."""
        shortener = URLShortener()

        with pytest.raises(URLValidationError):
            shortener.shorten("")

        with pytest.raises(URLValidationError):
            shortener.shorten("   ")

        with pytest.raises(URLValidationError):
            shortener.shorten("no-scheme.com/path")

        with pytest.raises(URLValidationError):
            shortener.shorten("http://")


class TestResolve:
    """Tests for URLShortener.resolve()."""

    def test_resolve_returns_correct_long_url_and_redirect_type(self):
        """Resolving a short code returns the original URL and redirect type."""
        shortener = URLShortener()
        url = "https://example.com/resolve-test"

        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]
        long_url, redirect_type = shortener.resolve(short_code)

        assert long_url == url
        assert redirect_type == RedirectType.TEMPORARY

    def test_resolve_unknown_code_raises_short_code_not_found_error(self):
        """Resolving a non-existent short code raises ShortCodeNotFoundError."""
        shortener = URLShortener()

        with pytest.raises(ShortCodeNotFoundError):
            shortener.resolve("aaaaaaa")

    def test_resolve_records_click_analytics(self):
        """Each resolve call records a click for analytics."""
        shortener = URLShortener()
        url = "https://example.com/click-test"

        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]

        shortener.resolve(short_code, client_id="user-1")

        records = shortener.get_click_records(short_code)
        assert len(records) == 1
        assert records[0].short_code == short_code
        assert records[0].client_id == "user-1"
        assert records[0].timestamp is not None


class TestClickTracking:
    """Tests for click count and click records."""

    def test_get_click_count_returns_correct_count(self):
        """Click count matches the number of resolve calls."""
        shortener = URLShortener()
        url = "https://example.com/count-test"

        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]

        assert shortener.get_click_count(short_code) == 0

        shortener.resolve(short_code)
        shortener.resolve(short_code)
        shortener.resolve(short_code)

        assert shortener.get_click_count(short_code) == 3

    def test_get_click_records_returns_correct_records(self):
        """Click records contain correct data for each resolve call."""
        shortener = URLShortener()
        url = "https://example.com/records-test"

        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]

        # No clicks yet
        assert shortener.get_click_records(short_code) == []

        shortener.resolve(short_code, client_id="client-a")
        shortener.resolve(short_code, client_id="client-b")

        records = shortener.get_click_records(short_code)
        assert len(records) == 2
        assert records[0].client_id == "client-a"
        assert records[1].client_id == "client-b"
        assert all(r.short_code == short_code for r in records)

    def test_get_click_count_returns_zero_for_no_clicks(self):
        """Click count is zero for a code that has never been resolved."""
        shortener = URLShortener()
        assert shortener.get_click_count("unknown") == 0

    def test_get_click_records_returns_empty_for_no_clicks(self):
        """Click records is empty for a code that has never been resolved."""
        shortener = URLShortener()
        assert shortener.get_click_records("unknown") == []


class TestStrategyPluggability:
    """Tests for strategy selection and pluggability."""

    def test_default_strategy_is_hash_collision_strategy(self):
        """URLShortener defaults to HashCollisionStrategy when none specified."""
        shortener = URLShortener()
        assert isinstance(shortener._strategy, HashCollisionStrategy)

    def test_custom_strategy_is_accepted(self):
        """URLShortener accepts a custom strategy at construction time."""
        strategy = Base62Strategy()
        shortener = URLShortener(strategy=strategy)
        assert shortener._strategy is strategy

        # Verify it works with the custom strategy
        url = "https://example.com/base62-test"
        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]
        assert len(short_code) == 7
