"""Robots.txt compliance checker with per-host caching."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse
import urllib.robotparser
from typing import Optional

import web_crawler.fetcher
from web_crawler import exceptions


class RobotsChecker:
    """Checks robots.txt compliance with per-host caching.

    Fetches and parses robots.txt for each host on first access, caches the
    result for the configured TTL period.
    """

    def __init__(
        self,
        fetcher: web_crawler.fetcher.Fetcher,
        user_agent: str = "WebCrawler/1.0",
        cache_ttl: float = 3600.0,
    ) -> None:
        self._locks = [asyncio.Lock() for _ in range(64)]
        self._fetcher = fetcher
        self._user_agent = user_agent
        self._cache_ttl = cache_ttl
        # host -> (RobotFileParser or None, timestamp)
        self._cache: dict[
            str, tuple[Optional[urllib.robotparser.RobotFileParser], float]
        ] = {}

    async def is_allowed(self, url: str) -> bool:
        """Check if a URL is allowed by robots.txt.

        Args:
            url: The URL to check.

        Returns:
            True if allowed, False if disallowed.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        robots_url = f"{parsed.scheme}://{host}/robots.txt"

        origin = f"{parsed.scheme}://{host}"
        index = int.from_bytes(hashlib.sha256(origin.encode()).digest()[:4], "big") % 64
        async with self._locks[index]:
            parser = await self._get_parser(origin, robots_url)
        if parser is None:
            return True  # If robots.txt unavailable, allow all
        return self._allows(parser, url)

    def _allows(self, parser, url: str) -> bool:
        """Extend stdlib groups with wildcard/end-anchor and longest-rule matching.

        The book includes rules such as /rss/people/*/reviews. stdlib's plain
        prefix matching cannot enforce them. Allow wins equal-length matches.
        Other group selection and failure policies retain stdlib semantics.
        """
        entry = next(
            (entry for entry in parser.entries if entry.applies_to(self._user_agent)),
            parser.default_entry,
        )
        if entry is None:
            return True
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
        best = (-1, True)
        for rule in entry.rulelines:
            # Undo only RobotFileParser's quoting, retaining original %xx escapes.
            pattern = urllib.parse.unquote(rule.path)
            anchored = pattern.endswith("$")
            if anchored:
                pattern = pattern[:-1]
            pattern = "*".join(_canonical_path(part) for part in pattern.split("*"))
            if _matches_rule(pattern, path, anchored):
                best = max(best, (len(pattern.replace("*", "")), rule.allowance))
        return best[1]

    def crawl_delay(self, url: str) -> float:
        parsed = urllib.parse.urlparse(url)
        key = f"{parsed.scheme}://{parsed.netloc.lower()}"
        parser, _ = self._cache.get(key, (None, 0))
        return float(parser.crawl_delay(self._user_agent) or 0) if parser else 0.0

    async def _get_parser(
        self, host: str, robots_url: str
    ) -> Optional[urllib.robotparser.RobotFileParser]:
        """Get or fetch the robots.txt parser for a host.

        Checks the cache first. If the cached entry is still within TTL,
        returns it directly. Otherwise fetches robots.txt, parses it,
        and stores the result in the cache.

        Args:
            host: The host (netloc) to look up.
            robots_url: The full URL to the robots.txt file.

        Returns:
            A RobotFileParser if robots.txt was successfully fetched and parsed,
            or None if it could not be fetched (in which case all paths are
            allowed).
        """
        now = time.monotonic()

        # Check cache
        if host in self._cache:
            parser, cached_at = self._cache[host]
            if now - cached_at < self._cache_ttl:
                return parser

        # Fetch robots.txt
        try:
            response = await self._fetcher.fetch(robots_url, timeout=10.0)
            parser = urllib.robotparser.RobotFileParser()
            lines = response.body.decode("utf-8", errors="replace").splitlines()
            for index, line in enumerate(lines):
                key, separator, value = line.partition(":")
                if separator and key.strip().lower() in ("allow", "disallow"):
                    # Stdlib unquotes directives before storing them. Protect
                    # literal escapes so %2A/%24 do not become rule operators.
                    lines[index] = key + ":" + value.replace("%", "%25")
            parser.parse(lines)
            # RobotFileParser strips the request's product version. Normalize
            # versioned directives too so their restrictions are not bypassed.
            for entry in parser.entries:
                entry.useragents = [
                    agent.split("/", 1)[0] for agent in entry.useragents
                ]
            self._cache[host] = (parser, now)
            return parser
        except (exceptions.FetchError, OSError):
            # If robots.txt cannot be fetched, allow all paths
            self._cache[host] = (None, now)
            return None


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
