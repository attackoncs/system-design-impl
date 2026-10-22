"""Property-based tests for the URL shortener library using Hypothesis.

These tests validate universal correctness properties that must hold across all
valid inputs, as defined in the design document.
"""

import re
import string

import hypothesis
import hypothesis.strategies
import pytest
import url_shortener

# --- Strategies ---

BASE62_ALPHABET = (
    string.digits + string.ascii_lowercase + string.ascii_uppercase
)
BASE62_PATTERN = re.compile(r"^[0-9a-zA-Z]{7}$")


@hypothesis.strategies.composite
def valid_urls(draw):
    """Generate valid URLs with http/https scheme and a netloc."""
    scheme = draw(hypothesis.strategies.sampled_from(["http", "https"]))
    domain = draw(
        hypothesis.strategies.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=1,
            max_size=20,
        )
    )
    tld = draw(hypothesis.strategies.sampled_from(["com", "org", "net", "io"]))
    path = draw(
        hypothesis.strategies.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789/-_",
            min_size=0,
            max_size=50,
        )
    )
    return f"{scheme}://{domain}.{tld}/{path}"


@hypothesis.strategies.composite
def invalid_urls_empty(draw):
    """Generate empty or whitespace-only strings."""
    spaces = draw(hypothesis.strategies.text(alphabet=" \t\n\r", min_size=0, max_size=10))
    return spaces


@hypothesis.strategies.composite
def invalid_urls_no_scheme(draw):
    """Generate URLs missing a scheme."""
    domain = draw(
        hypothesis.strategies.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=1,
            max_size=20,
        )
    )
    tld = draw(hypothesis.strategies.sampled_from(["com", "org", "net", "io"]))
    return f"{domain}.{tld}/path"


@hypothesis.strategies.composite
def invalid_urls_no_netloc(draw):
    """Generate URLs with a scheme but missing netloc."""
    scheme = draw(hypothesis.strategies.sampled_from(["http", "https"]))
    return f"{scheme}://"


# --- Property 1: Short code format invariant ---


class TestShortCodeFormatInvariant:
    """Property 1: Short code format invariant.

    For any valid URL, both HashCollisionStrategy and Base62Strategy produce
    codes of exactly 7 chars from [0-9, a-z, A-Z].

    **Validates: Requirements 1.1, 3.2, 4.2, 4.3**
    """

    @hypothesis.given(url=valid_urls())
    @hypothesis.settings(max_examples=50)
    def test_hash_collision_strategy_format(self, url: str) -> None:
        """HashCollisionStrategy produces 7-char base-62 codes."""
        strategy = url_shortener.HashCollisionStrategy()
        storage = url_shortener.InMemoryStorage()
        code = strategy.generate(url, storage)

        assert len(code) == 7, f"Code '{code}' is not 7 characters"
        assert BASE62_PATTERN.match(
            code
        ), f"Code '{code}' contains invalid characters"

    @hypothesis.given(url=valid_urls())
    @hypothesis.settings(max_examples=50)
    def test_base62_strategy_format(self, url: str) -> None:
        """Base62Strategy produces 7-char base-62 codes."""
        strategy = url_shortener.Base62Strategy()
        storage = url_shortener.InMemoryStorage()
        code = strategy.generate(url, storage)

        assert len(code) == 7, f"Code '{code}' is not 7 characters"
        assert BASE62_PATTERN.match(
            code
        ), f"Code '{code}' contains invalid characters"


# --- Property 2: Shorten/resolve round-trip ---


class TestShortenResolveRoundTrip:
    """Property 2: Shorten/resolve round-trip.

    For any valid URL shortened, resolving the code returns the original URL.

    **Validates: Requirements 2.1, 5.3**
    """

    @hypothesis.given(url=valid_urls())
    @hypothesis.settings(max_examples=50)
    def test_round_trip(self, url: str) -> None:
        """Shortening then resolving returns the original URL."""
        shortener = url_shortener.URLShortener()
        short_url = shortener.shorten(url)

        # Extract the short code from the full URL
        short_code = short_url.split("/")[-1]
        resolved_url, _ = shortener.resolve(short_code)

        assert resolved_url == url


# --- Property 3: Shortening idempotence ---


class TestShorteningIdempotence:
    """Property 3: Shortening idempotence.

    Shortening the same URL multiple times returns the same short URL.

    **Validates: Requirements 1.3**
    """

    @hypothesis.given(url=valid_urls())
    @hypothesis.settings(max_examples=50)
    def test_idempotence(self, url: str) -> None:
        """Shortening the same URL twice yields the same result."""
        shortener = url_shortener.URLShortener()
        first = shortener.shorten(url)
        second = shortener.shorten(url)
        third = shortener.shorten(url)

        assert first == second == third


# --- Property 4: Invalid URL rejection ---


class TestInvalidURLRejection:
    """Property 4: Invalid URL rejection.

    Empty strings, whitespace-only, missing scheme, missing netloc all raise
    URLValidationError.

    **Validates: Requirements 1.4**
    """

    @hypothesis.given(url=invalid_urls_empty())
    @hypothesis.settings(max_examples=50)
    def test_empty_or_whitespace_rejected(self, url: str) -> None:
        """Empty or whitespace-only strings raise URLValidationError."""
        shortener = url_shortener.URLShortener()
        with pytest.raises(url_shortener.URLValidationError):
            shortener.shorten(url)

    @hypothesis.given(url=invalid_urls_no_scheme())
    @hypothesis.settings(max_examples=50)
    def test_missing_scheme_rejected(self, url: str) -> None:
        """URLs without a scheme raise URLValidationError."""
        shortener = url_shortener.URLShortener()
        with pytest.raises(url_shortener.URLValidationError):
            shortener.shorten(url)

    @hypothesis.given(url=invalid_urls_no_netloc())
    @hypothesis.settings(max_examples=50)
    def test_missing_netloc_rejected(self, url: str) -> None:
        """URLs without a netloc raise URLValidationError."""
        shortener = url_shortener.URLShortener()
        with pytest.raises(url_shortener.URLValidationError):
            shortener.shorten(url)


# --- Property 5: Non-existent code error ---


class TestNonExistentCodeError:
    """Property 5: Non-existent code error.

    Any 7-char base-62 string not in storage raises ShortCodeNotFoundError on
    resolve.

    **Validates: Requirements 2.2**
    """

    @hypothesis.given(
        code=hypothesis.strategies.text(
            alphabet=BASE62_ALPHABET,
            min_size=7,
            max_size=7,
        )
    )
    @hypothesis.settings(max_examples=50)
    def test_non_existent_code_raises(self, code: str) -> None:
        """Resolving a code not in storage raises ShortCodeNotFoundError."""
        shortener = url_shortener.URLShortener()
        with pytest.raises(url_shortener.ShortCodeNotFoundError):
            shortener.resolve(code)


# --- Property 6: Redirect type preservation ---


class TestRedirectTypePreservation:
    """Property 6: Redirect type preservation.

    Shortening with a specified redirect type preserves it on resolve; default
    is 302.

    **Validates: Requirements 1.5, 1.6, 2.4, 5.4**
    """

    @hypothesis.given(
        url=valid_urls(),
        redirect_type=hypothesis.strategies.sampled_from(
            [url_shortener.RedirectType.PERMANENT, url_shortener.RedirectType.TEMPORARY]
        ),
    )
    @hypothesis.settings(max_examples=50)
    def test_specified_redirect_type_preserved(
        self, url: str, redirect_type: url_shortener.RedirectType
    ) -> None:
        """Specified redirect type is preserved on resolve."""
        shortener = url_shortener.URLShortener()
        short_url = shortener.shorten(url, redirect_type=redirect_type)

        short_code = short_url.split("/")[-1]
        _, resolved_type = shortener.resolve(short_code)

        assert resolved_type == redirect_type

    @hypothesis.given(url=valid_urls())
    @hypothesis.settings(max_examples=50)
    def test_default_redirect_type_is_302(self, url: str) -> None:
        """Default redirect type is 302 (temporary)."""
        shortener = url_shortener.URLShortener()
        short_url = shortener.shorten(url)

        short_code = short_url.split("/")[-1]
        _, resolved_type = shortener.resolve(short_code)

        assert resolved_type == url_shortener.RedirectType.TEMPORARY
        assert resolved_type == 302


# --- Property 7: Collision resolution uniqueness ---


class TestCollisionResolutionUniqueness:
    """Property 7: Collision resolution uniqueness.

    Distinct URLs shortened via HashCollisionStrategy receive distinct short
    codes.

    **Validates: Requirements 3.3, 3.4**
    """

    @hypothesis.given(
        urls=hypothesis.strategies.lists(
            valid_urls(),
            min_size=2,
            max_size=5,
            unique=True,
        )
    )
    @hypothesis.settings(max_examples=50)
    def test_distinct_urls_get_distinct_codes(self, urls: list[str]) -> None:
        """Each distinct URL gets a unique short code."""
        strategy = url_shortener.HashCollisionStrategy()
        storage = url_shortener.InMemoryStorage()
        shortener = url_shortener.URLShortener(strategy=strategy, storage=storage)

        short_urls = [shortener.shorten(url) for url in urls]

        # All short URLs should be distinct
        assert len(set(short_urls)) == len(urls)


# --- Property 8: Click tracking accuracy ---


class TestClickTrackingAccuracy:
    """Property 8: Click tracking accuracy.

    Resolving a code N times yields click count == N and N click records.

    **Validates: Requirements 2.3, 6.1, 6.2, 6.3**
    """

    @hypothesis.given(
        url=valid_urls(),
        n=hypothesis.strategies.integers(min_value=1, max_value=20),
    )
    @hypothesis.settings(max_examples=50)
    def test_click_count_matches_resolves(self, url: str, n: int) -> None:
        """Resolving N times produces exactly N clicks and N records."""
        shortener = url_shortener.URLShortener()
        short_url = shortener.shorten(url)
        short_code = short_url.split("/")[-1]

        for _ in range(n):
            shortener.resolve(short_code)

        assert shortener.get_click_count(short_code) == n

        records = shortener.get_click_records(short_code)
        assert len(records) == n

        # Each record has the correct short code and a valid timestamp
        for record in records:
            assert record.short_code == short_code
            assert record.timestamp is not None


# --- Property 9: Base-62 encoding round-trip ---


class TestBase62EncodingRoundTrip:
    """Property 9: Base-62 encoding round-trip.

    For any integer in [0, 62^7 - 1], encode then decode returns
    the original.

    **Validates: Requirements 4.2**
    """

    @hypothesis.given(n=hypothesis.strategies.integers(min_value=0, max_value=62**7 - 1))
    @hypothesis.settings(max_examples=50)
    def test_encode_decode_round_trip(self, n: int) -> None:
        """Encoding then decoding returns the original integer."""
        encoded = url_shortener.encode_base62(n)
        decoded = url_shortener.decode_base62(encoded)

        assert decoded == n
