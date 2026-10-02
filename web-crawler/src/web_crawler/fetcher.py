"""HTTP fetcher interface and default urllib implementation."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from web_crawler.exceptions import FetchError


@dataclass
class FetchResponse:
    """Response from an HTTP fetch operation."""

    status_code: int
    headers: Dict[str, str]
    body: bytes
    url: str


class Fetcher(ABC):
    """Abstract interface for HTTP fetching."""

    @abstractmethod
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
        ...


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
        req = Request(url, headers=headers)
        try:
            response = urlopen(req, timeout=timeout)
            resp_headers = {k: v for k, v in response.getheaders()}
            body = response.read()
            return FetchResponse(
                status_code=response.status,
                headers=resp_headers,
                body=body,
                url=url,
            )
        except HTTPError as e:
            raise FetchError(url, f"HTTP {e.code}: {e.reason}")
        except URLError as e:
            raise FetchError(url, str(e.reason))
        except TimeoutError:
            raise FetchError(url, "Request timed out")
