"""Custom exception hierarchy for the URL shortener library."""


class URLShortenerError(Exception):
    """Base exception for all URL shortener errors."""

    pass


class URLValidationError(URLShortenerError):
    """Raised when an invalid URL is submitted for shortening.

    This includes empty strings, missing schemes, missing netloc,
    or otherwise malformed URL formats.
    """

    pass


class ShortCodeNotFoundError(URLShortenerError):
    """Raised when a short code does not exist in storage.

    This occurs when attempting to resolve or look up a short code
    that has not been previously created.
    """

    pass


class CollisionLimitExceededError(URLShortenerError):
    """Raised when hash collision resolution exceeds the maximum retry limit.

    This occurs in the hash+collision strategy when repeated rehashing
    fails to produce a unique short code within the configured number
    of attempts.
    """

    pass
