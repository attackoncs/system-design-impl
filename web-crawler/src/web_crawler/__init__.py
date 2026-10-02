"""Web Crawler - An asyncio-based web crawling library."""

from web_crawler.config import CrawlerConfig
from web_crawler.crawler import Crawler
from web_crawler.dedup import ContentSeen, URLSeen
from web_crawler.exceptions import (
    ConfigurationError,
    ContentParseError,
    CrawlerError,
    FetchError,
    RobotsDisallowedError,
)
from web_crawler.fetcher import Fetcher, FetchResponse, URLLibFetcher
from web_crawler.filters import URLFilter
from web_crawler.frontier import URLFrontier
from web_crawler.models import CrawlResult, CrawlStatus, FrontierEntry, Priority
from web_crawler.parser import ContentParser, LinkExtractor, normalize_url
from web_crawler.robots import RobotsChecker

__all__ = [
    "Crawler", "CrawlerConfig",
    "CrawlResult", "CrawlStatus", "FrontierEntry", "Priority",
    "URLFrontier",
    "Fetcher", "FetchResponse", "URLLibFetcher",
    "RobotsChecker",
    "ContentParser", "LinkExtractor", "normalize_url",
    "URLFilter",
    "ContentSeen", "URLSeen",
    "CrawlerError", "ConfigurationError", "ContentParseError",
    "FetchError", "RobotsDisallowedError",
]
