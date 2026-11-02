"""Service endpoint validation and ownership of urllib error responses."""

import contextlib
import urllib.error
from urllib.parse import SplitResult, urlsplit


def parse_endpoint(value: str, schemes: tuple[str, ...]) -> SplitResult:
    """Validate transport syntax without rewriting or contacting the endpoint."""
    if (
        not isinstance(value, str)
        or not value
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
            for char in value
        )
    ):
        raise ValueError("invalid endpoint")
    value.encode("utf-8")
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ValueError("invalid endpoint")
    _ = parsed.port  # Force validation of numeric syntax and port range.
    return parsed


@contextlib.contextmanager
def open_response(opener, target, timeout):
    """Close successful responses and HTTPError bodies, preserving HTTP errors.

    HTTPError can own a response stream even though open() did not return.
    """
    try:
        response = opener(target, timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        raise
    with response:
        yield response
