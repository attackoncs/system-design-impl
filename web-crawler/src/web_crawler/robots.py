"""Robots.txt compliance checker with per-host caching."""

from __future__ import annotations

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

        parser = await self._get_parser(host, robots_url)
        if parser is None:
            return True  # If robots.txt unavailable, allow all
        return parser.can_fetch(self._user_agent, url)

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
            parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
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
