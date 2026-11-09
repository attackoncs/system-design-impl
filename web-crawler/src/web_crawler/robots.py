"""Robots.txt (RFC 9309) compliance checker with per-origin caching."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
import re
import time
import urllib.parse
from typing import Optional

import web_crawler.fetcher
from web_crawler import exceptions

MAX_ROBOTS_BYTES = 500 * 1024  # RFC 9309 2.5: parse at least 500 KiB
MAX_ROBOTS_REDIRECTS = 5  # RFC 9309 2.3.1.2: follow at least five redirects
MAX_ROBOTS_CACHE_TTL = 24 * 3600.0  # RFC 9309 2.4: reuse at most 24 hours
UNREACHABLE_RETRY_SECONDS = 60.0  # retry interval after 5xx/network failure


@dataclasses.dataclass(frozen=True)
class _Rules:
    """Rules of the group(s) selected for this crawler's product token."""

    rules: tuple = ()  # (allow, canonical pattern, anchored, specificity)
    crawl_delay: float = 0.0
    disallow_all: bool = False


_ALLOW_ALL = _Rules()
_DISALLOW_ALL = _Rules(disallow_all=True)
_SERVER_ERROR = object()
_NETWORK_ERROR = object()


class RobotsChecker:
    """Checks robots.txt compliance with per-origin caching (RFC 9309).

    Groups naming the crawler's product token (case-insensitive) are merged;
    the ``*`` groups are merged and used only when no specific group matches.
    2xx bodies are parsed (first 500 KiB), up to five redirects are followed,
    3xx without a target, more than five redirects and 4xx mean "no
    restrictions", and 5xx means complete disallow. Network failures follow
    ``unreachable_policy``: ``"allow"`` keeps the documented compatibility
    behavior, ``"disallow"`` is the RFC 9309 policy. While unreachable, a
    previously downloaded result keeps being used; failures retry sooner.
    """

    def __init__(
        self,
        fetcher: web_crawler.fetcher.Fetcher,
        user_agent: str = "WebCrawler/1.0",
        cache_ttl: float = 3600.0,
        unreachable_policy: str = "allow",
    ) -> None:
        if unreachable_policy not in ("allow", "disallow"):
            raise ValueError("unreachable_policy must be 'allow' or 'disallow'")
        self._locks = [asyncio.Lock() for _ in range(64)]
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._token = _product_token(user_agent)
        self._cache_ttl = min(cache_ttl, MAX_ROBOTS_CACHE_TTL)
        self._unreachable_policy = unreachable_policy
        # origin -> (rules, expires_at, reusable while the origin is unreachable)
        self._cache: dict[str, tuple[_Rules, float, bool]] = {}

    async def is_allowed(self, url: str) -> bool:
        """Return True if robots.txt allows this crawler to fetch ``url``."""
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
        index = int.from_bytes(hashlib.sha256(origin.encode()).digest()[:4], "big") % 64
        async with self._locks[index]:
            rules = await self._get_rules(origin)
        return self._allows(rules, url)

    def _allows(self, rules: _Rules, url: str) -> bool:
        """Longest match wins; Allow wins equal-length matches (RFC 9309 2.2.2)."""
        # Parse before normalizing escapes: %23 belongs to the path, not a fragment.
        parsed = urllib.parse.urlparse(url)
        path = (
            _canonical_path(
                urllib.parse.urlunparse(
                    ("", "", parsed.path or "/", parsed.params, parsed.query, "")
                )
            )
            or "/"
        )
        if path == "/robots.txt":
            return True  # implicitly allowed (RFC 9309 2.2.2)
        if rules.disallow_all:
            return False
        best = (-1, True)
        for allow, pattern, anchored, specificity in rules.rules:
            if specificity >= best[0] and _matches_rule(pattern, path, anchored):
                best = max(best, (specificity, allow))
        return best[1]

    def crawl_delay(self, url: str) -> float:
        parsed = urllib.parse.urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc.lower()}"
        cached = self._cache.get(key)
        return cached[0].crawl_delay if cached else 0.0

    async def _get_rules(self, origin: str) -> _Rules:
        """Return cached rules, downloading robots.txt when absent or expired."""
        now = time.monotonic()
        cached = self._cache.get(origin)
        if cached is not None and now < cached[1]:
            return cached[0]
        result = await self._download(origin + "/robots.txt")
        if isinstance(result, _Rules):
            self._cache[origin] = (result, now + self._cache_ttl, True)
            return result
        if cached is not None and cached[2]:
            # RFC 9309 2.4: a cached copy may outlive 24h while unreachable.
            rules, reusable = cached[0], True
        elif result is _NETWORK_ERROR and self._unreachable_policy == "allow":
            rules, reusable = _ALLOW_ALL, False
        else:
            rules, reusable = _DISALLOW_ALL, False
        retry = min(self._cache_ttl, UNREACHABLE_RETRY_SECONDS)
        self._cache[origin] = (rules, now + retry, reusable)
        return rules

    async def _download(self, url: str):
        """Map the robots.txt response to rules or an unreachable marker."""
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            try:
                response = await self._fetcher.fetch(url, timeout=10.0)
            except (exceptions.FetchError, OSError) as error:
                status = getattr(error, "status_code", None)
                if status is None:
                    return _NETWORK_ERROR
                return _ALLOW_ALL if 400 <= status < 500 else _SERVER_ERROR
            status = response.status_code
            location = next(
                (v for k, v in response.headers.items() if k.lower() == "location"),
                None,
            )
            if 300 <= status < 400 and location:
                url = urllib.parse.urljoin(response.url or url, location)
                continue
            if 200 <= status < 300:
                return _parse(response.body[:MAX_ROBOTS_BYTES], self._token)
            # 3xx without a target and 4xx: robots.txt is unavailable.
            return _ALLOW_ALL if 300 <= status < 500 else _SERVER_ERROR
        return _ALLOW_ALL  # more than five redirects: treat as unavailable


def _product_token(user_agent: str) -> str:
    """Lower-case product token: ``WebCrawler/1.0`` -> ``webcrawler``."""
    return user_agent.split("/", 1)[0].strip().lower()


def _parse(body: bytes, token: str) -> _Rules:
    """Parse robots.txt and merge the groups that apply to ``token``."""
    groups = []  # [agents, rules, delays]
    current = None
    in_rules = False
    for raw in body.decode("utf-8-sig", errors="replace").splitlines():
        key, separator, value = raw.split("#", 1)[0].partition(":")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            # Consecutive user-agent lines share one group; a user-agent line
            # after rules starts a new group. Blank lines do not end groups.
            if current is None or in_rules:
                current = [set(), [], []]
                groups.append(current)
                in_rules = False
            current[0].add(_product_token(value))
        elif current is None:
            continue  # rules before any user-agent line belong to no group
        elif key in ("allow", "disallow"):
            in_rules = True
            if value:  # an empty value is not a rule
                current[1].append((key == "allow", value))
        elif key == "crawl-delay":
            in_rules = True
            try:
                delay = float(value)
            except ValueError:
                continue
            if math.isfinite(delay) and delay >= 0:
                current[2].append(delay)
    selected = [group for group in groups if token in group[0]] or [
        group for group in groups if "*" in group[0]
    ]
    rules = []
    for allow, value in (rule for group in selected for rule in group[1]):
        anchored = value.endswith("$")
        pattern = value[:-1] if anchored else value
        pattern = "*".join(_canonical_path(part) for part in pattern.split("*"))
        rules.append((allow, pattern, anchored, len(pattern.replace("*", ""))))
    delays = [delay for group in selected for delay in group[2]]
    return _Rules(tuple(rules), max(delays, default=0.0))


def _canonical_path(path: str) -> str:
    """Normalize URI octets while retaining escaped reserved characters."""

    def normalize_escape(match):
        character = chr(int(match[1], 16))
        if character.isascii() and (character.isalnum() or character in "-._~"):
            return character
        return "%" + match[1].upper()

    encoded = urllib.parse.quote(path, safe="/%?=:;@!&'()+,-._~")
    return re.sub(r"%([0-9a-fA-F]{2})", normalize_escape, encoded)


def _matches_rule(pattern: str, path: str, anchored: bool) -> bool:
    """Match robots '*' with ordered substring searches, without regex backtracking."""
    parts = pattern.split("*")
    if not path.startswith(parts[0]):
        return False
    if len(parts) == 1:
        return not anchored or len(path) == len(pattern)
    position = len(parts[0])
    for part in parts[1:-1]:
        found = path.find(part, position)
        if found < 0:
            return False
        position = found + len(part)
    if anchored:
        return path.endswith(parts[-1]) and len(path) - len(parts[-1]) >= position
    return path.find(parts[-1], position) >= 0
