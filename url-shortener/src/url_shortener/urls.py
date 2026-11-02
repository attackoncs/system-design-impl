"""Validate stored URLs and encode international URLs for HTTP headers."""

from unicodedata import normalize
from urllib.parse import quote, urlsplit

from url_shortener.exceptions import URLValidationError


def redirect_uri(url: str) -> str:
    """Return an ASCII HTTP(S) URI without changing the stored URL identity.

    Existing percent escapes remain intact. No DNS lookup or remote fetch occurs.
    """
    if (
        not isinstance(url, str)
        or not url
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
            for char in url
        )
    ):
        raise URLValidationError(
            "URL must be nonempty and contain no whitespace or controls"
        )
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("URL requires an http(s) scheme and a hostname")
        port = parts.port  # Validate numeric syntax and the 0..65535 range.
        hostname = parts.hostname
        if parts.netloc.rsplit("@", 1)[-1].startswith("["):
            host = f"[{hostname}]"  # Preserve IPv6 and IPvFuture host syntax.
        else:
            host = hostname.encode("idna").decode("ascii")
            if not hostname.isascii() and host.encode("ascii").decode(
                "idna"
            ) != normalize("NFC", hostname):
                # stdlib IDNA2003 can fold distinct modern hosts, e.g. ß -> ss.
                raise ValueError(
                    "lossy IDNA conversion; supply the explicit ASCII ACE hostname"
                )
        userinfo, separator, _ = parts.netloc.rpartition("@")
        authority = (quote(userinfo, safe=":!$&'()*+,;=%") + "@") if separator else ""
        authority += host + (f":{port}" if port is not None else "")
        uri = f"{parts.scheme}://{authority}{parts.path}"
        # Empty delimiters carry meaning: /? differs from /, and /# clears a
        # fragment instead of inheriting the short URL's fragment on redirect.
        if "?" in url.partition("#")[0]:
            uri += "?" + parts.query
        if "#" in url:
            uri += "#" + parts.fragment
        return quote(uri, safe=":/?#[]@!$&'()*+,;=%")
    except (ValueError, UnicodeError) as error:
        raise URLValidationError(f"Invalid HTTP(S) URL: {error}") from error
