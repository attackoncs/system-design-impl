"""Key resolution functions for rate limiting.

This module provides functions to generate rate limiting keys from various
request attributes such as IP address, user ID, and custom combinations.
"""

from __future__ import annotations

import logging
import hashlib
from urllib.parse import urlsplit
from typing import Any, Callable

# Type alias for key functions
KeyFunc = Callable[[Any], str]


def ip_key(request: Any, *, trust_forwarded: bool = False) -> str:
    """Use the connection peer by default.

    Opt in to X-Forwarded-For only behind a proxy that strips client-supplied
    forwarding headers. Prefer the framework's trusted-proxy middleware.
    """
    headers = getattr(request, "headers", None)
    if trust_forwarded and headers is not None:
        forwarded = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        if isinstance(forwarded, str) and forwarded.strip():
            return forwarded.split(",", 1)[0].strip()
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if isinstance(host, str) and host:
        return host
    remote = getattr(request, "remote_addr", None)
    return remote if isinstance(remote, str) and remote else "127.0.0.1"


def user_id_key(request: Any) -> str:
    """Extract user ID from request for rate limiting.

    Looks for user ID in common locations depending on authentication method.

    Args:
        request: Request object with user info

    Returns:
        User ID as string for use as rate limiting key

    Example:
        For authenticated users: rate_limit(key_func=user_id_key)
    """
    # FastAPI with authentication dependency
    try:
        user = getattr(request, "user", None)
    except AssertionError:  # Starlette without AuthenticationMiddleware
        user = None
    if user is not None:
        if hasattr(user, "id") and isinstance(user.id, (str, int)):
            return str(user.id)

        # Flask-Login
        if hasattr(user, "is_authenticated") and user.is_authenticated:
            if hasattr(user, "id") and isinstance(user.id, (str, int)):
                return str(user.id)

    # JWT in Authorization header
    headers = getattr(request, "headers", None)
    if headers is not None and hasattr(headers, "get") and callable(headers.get):
        auth_header = headers.get("Authorization") or headers.get("authorization")
        if (
            auth_header
            and isinstance(auth_header, str)
            and auth_header.startswith("Bearer ")
        ):
            # In real implementation, you'd decode the JWT here
            # For now, just use the token as identifier
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            return "jwt:" + hashlib.sha256(token.encode()).hexdigest()

    # API key in header
    if headers is not None and hasattr(headers, "get") and callable(headers.get):
        api_key = headers.get("X-API-Key") or headers.get("x-api-key")
        if api_key and isinstance(api_key, str):
            return "api_key:" + hashlib.sha256(api_key.encode()).hexdigest()

    # Fallback to IP-based identification
    return f"anonymous:{ip_key(request)}"


def composite_key(*key_funcs: KeyFunc) -> KeyFunc:
    """Create a composite key function from multiple key functions.

    Combines multiple key extraction functions into a single key.
    Useful for creating more specific rate limiting rules.

    Args:
        *key_funcs: Variable number of key functions to combine

    Returns:
        A new key function that combines all input functions

    Example:
        # Rate limit by both IP and user ID
        user_ip_key = composite_key(ip_key, user_id_key)
        rate_limit(key_func=user_ip_key)

        # Rate limit by endpoint and IP
        def endpoint_key(request):
            return request.path
        endpoint_ip_key = composite_key(endpoint_key, ip_key)
    """

    def combined_key_func(request: Any) -> str:
        key_parts = []
        for key_func in key_funcs:
            try:
                part = key_func(request)
                key_parts.append(str(part))
            # Isolate this operation; record failures before continuing.
            except Exception:  # pylint: disable=broad-exception-caught
                # If any key function fails, use a safe fallback
                logging.getLogger(__name__).exception(
                    "Failure isolated in %s", "combined_key_func"
                )
                key_parts.append("error")

        return ":".join(key_parts)

    return combined_key_func


def path_key(request: Any) -> str:
    """Resolve only the URL path; query parameters must not create new quotas."""
    path = getattr(request, "path", None)
    if isinstance(path, str):
        return path.split("?", 1)[0]
    url = getattr(request, "url", None)
    if url is not None:
        return urlsplit(str(url)).path or "/"
    full_path = getattr(request, "full_path", None)
    if isinstance(full_path, str):
        return full_path.split("?", 1)[0]
    return "/unknown"


def method_key(request: Any) -> str:
    """Extract HTTP method for rate limiting.

    Args:
        request: Request object with method info

    Returns:
        HTTP method as string for use as rate limiting key
    """
    method = getattr(request, "method", None)
    if method is not None:
        return method.upper()

    request_method = getattr(request, "request_method", None)
    if request_method is not None:
        return request_method.upper()

    return "UNKNOWN"


def header_key(header_name: str) -> KeyFunc:
    """Create a key function that extracts a specific header.

    Args:
        header_name: Name of the header to extract

    Returns:
        Key function that extracts the specified header

    Example:
        # Rate limit by API version
        version_key = header_key('X-API-Version')
        rate_limit(key_func=version_key)
    """

    def extract_header(request: Any) -> str:
        if hasattr(request, "headers"):
            value = request.headers.get(header_name) or request.headers.get(
                header_name.lower()
            )
            return value or f"no_{header_name}"

        return f"no_{header_name}"

    return extract_header


def rate_limit_key(
    ip: bool = True,
    user: bool = False,
    path: bool = False,
    method: bool = False,
    custom_funcs: list[KeyFunc] | None = None,
) -> KeyFunc:
    """Create a flexible rate limiting key function.

    Combines commonly used key components based on parameters.

    Args:
        ip: Include IP address in key
        user: Include user ID in key
        path: Include request path in key
        method: Include HTTP method in key
        custom_funcs: Additional custom key functions to include

    Returns:
        Combined key function

    Example:
        # Rate limit by IP and path
        key_func = rate_limit_key(ip=True, path=True)

        # Rate limit by user, path, and method
        key_func = rate_limit_key(user=True, path=True, method=True)

        # Rate limit by IP with custom header
        custom_header = header_key('X-Tenant-ID')
        key_func = rate_limit_key(ip=True, custom_funcs=[custom_header])
    """
    key_funcs = []

    if ip:
        key_funcs.append(ip_key)

    if user:
        key_funcs.append(user_id_key)

    if path:
        key_funcs.append(path_key)

    if method:
        key_funcs.append(method_key)

    if custom_funcs:
        key_funcs.extend(custom_funcs)

    # If no functions specified but ip is True (default), use only ip
    # If user explicitly sets ip=False and specifies other functions, don't add
    # ip
    if not key_funcs:
        key_funcs.append(ip_key)

    # Use the first key function directly if only one is specified
    if len(key_funcs) == 1:
        return key_funcs[0]

    return composite_key(*key_funcs)
