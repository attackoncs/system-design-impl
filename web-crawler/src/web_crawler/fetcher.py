"""HTTP fetcher interface and default urllib implementation."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import urllib.error
import urllib.request
from typing import Dict, Optional

import web_crawler.exceptions


@dataclasses.dataclass
class FetchResponse:
    """Response from an HTTP fetch operation."""

    status_code: int
    headers: Dict[str, str]
    body: bytes
    url: str


class Fetcher(abc.ABC):
    """Abstract interface for HTTP fetching."""

    @abc.abstractmethod
    async def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch a URL and return the response.

        Args:
            url: The URL to fetch.
            headers: Optional request headers.
            timeout: Request timeout in seconds.

        Returns:
            FetchResponse with status, headers, and body.

        Raises:
            FetchError: If the fetch fails.
        """


class URLLibFetcher(Fetcher):
    """Default fetcher using urllib.request wrapped in asyncio executor.

    Wraps blocking urllib calls in run_in_executor for async compatibility.
    """

    async def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """Fetch URL using urllib in a thread executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._blocking_fetch, url, headers or {}, timeout
        )

    def _blocking_fetch(
        self,
        url: str,
        headers: Dict[str, str],
        timeout: float,
    ) -> FetchResponse:
        """Synchronous fetch using urllib.request."""
        req = urllib.request.Request(url, headers=headers)
        try:
            with contextlib.closing(
                urllib.request.urlopen(req, timeout=timeout)
            ) as response:
                return FetchResponse(
                    status_code=response.status,
                    headers=dict(response.getheaders()),
                    body=response.read(),
                    url=url,
                )
        except urllib.error.HTTPError as e:
            raise web_crawler.exceptions.FetchError(
                url, f"HTTP {e.code}: {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise web_crawler.exceptions.FetchError(url, str(e.reason)) from e
        except TimeoutError as e:
            raise web_crawler.exceptions.FetchError(
                url, "Request timed out"
            ) from e
