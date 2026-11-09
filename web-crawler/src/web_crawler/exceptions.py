"""Custom exceptions for the web crawler library."""


class CrawlerError(Exception):
    """Base exception for all web crawler errors."""

    pass


class ConfigurationError(CrawlerError):
    """Raised when crawler configuration is invalid."""

    pass


class FetchError(CrawlerError):
    """Raised when an HTTP fetch operation fails.

    ``status_code`` is set when the server answered with an HTTP error status
    and is ``None`` for network failures (robots.txt treats them differently).
    """

    def __init__(self, url: str, reason: str, status_code=None) -> None:
        self.url = url
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"Failed to fetch '{url}': {reason}")


class RobotsDisallowedError(CrawlerError):
    """Raised when a URL is disallowed by robots.txt."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"URL disallowed by robots.txt: '{url}'")


class ContentParseError(CrawlerError):
    """Raised when HTML content cannot be parsed."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"Failed to parse content from '{url}': {reason}")
