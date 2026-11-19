"""Canonical host identities for admission, politeness and worker placement.

These keys do not rewrite stored URLs or perform DNS lookups. In particular,
credentials and ports cannot create a second identity for the same hostname.
"""

import ipaddress
from urllib.parse import urlsplit


def canonical_hostname(host: str) -> str:
    """Normalize an exact DNS/IP host, accepting bracketed IPv6 configuration."""
    if (
        not isinstance(host, str)
        or not host
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "%\\/@?#"
            for char in host
        )
    ):
        raise ValueError("invalid hostname")
    host = host.rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        if not host or any(char in ":[]" for char in host):
            raise ValueError("invalid hostname") from None
        try:
            return host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as error:
            raise ValueError("invalid IDNA hostname") from error


def host_for_url(url: str) -> str:
    """Validate HTTP(S) authority and return its canonical hostname.

    urllib unquotes the authority before connecting. Reject escaped authorities
    rather than let filtering and transport disagree about the destination.
    Percent escapes in paths/queries remain supported.
    """
    if (
        not isinstance(url, str)
        or not url
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise ValueError("url must be a nonempty HTTP(S) URL without whitespace")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("url requires an HTTP(S) hostname")
    if "%" in parsed.netloc or "\\" in parsed.netloc:
        raise ValueError("escaped or ambiguous authorities are not supported")
    _ = parsed.port
    return canonical_hostname(parsed.hostname)


def origin_for_url(url: str) -> str:
    """Return scheme/host/port identity without credentials or default ports."""
    host = host_for_url(url)
    parsed = urlsplit(url)
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return f"{parsed.scheme}://{host}"
