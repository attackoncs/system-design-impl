"""Custom exceptions for the web crawler library."""


class CrawlerError(Exception):
    """Base exception for all web crawler errors."""

    pass


class ConfigurationError(CrawlerError):
    """Raised when crawler configuration is invalid."""

    pass


class FetchError(CrawlerError):
    """Raised when an HTTP fetch operation fails."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
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
