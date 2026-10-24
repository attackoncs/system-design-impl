"""Unit tests for fetcher module."""

import unittest.mock
import urllib.error

import pytest
import web_crawler.exceptions
import web_crawler.fetcher


class TestFetchResponse:
    """Test FetchResponse dataclass creation and field access."""

    def test_creation(self):
        resp = web_crawler.fetcher.FetchResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            body=b"<html></html>",
            url="http://example.com",
        )
        assert resp.status_code == 200
        assert resp.headers == {"Content-Type": "text/html"}
        assert resp.body == b"<html></html>"
        assert resp.url == "http://example.com"

    def test_creation_with_empty_body(self):
        resp = web_crawler.fetcher.FetchResponse(
            status_code=204,
            headers={},
            body=b"",
            url="http://example.com/empty",
        )
        assert resp.status_code == 204
        assert resp.headers == {}
        assert resp.body == b""
        assert resp.url == "http://example.com/empty"

    def test_creation_with_multiple_headers(self):
        headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": "1234",
            "Server": "nginx",
        }
        resp = web_crawler.fetcher.FetchResponse(
            status_code=200,
            headers=headers,
            body=b"hello",
            url="http://example.com/page",
        )
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        assert resp.headers["Content-Length"] == "1234"
        assert resp.headers["Server"] == "nginx"


class TestFetcherABC:
    """Test Fetcher is ABC (cannot instantiate)."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            web_crawler.fetcher.Fetcher()


class TestURLLibFetcherErrorHandling:
    """Test URLLibFetcher error handling with mocks (no real network calls).

    Validates Requirements 9.1, 9.2, 3.7:
    - Fetcher interface returns response (status code, headers, body)
    - Default implementation uses a private urllib opener
    - HTTP errors are converted to FetchError
    """

    @pytest.fixture
    def fetcher(self):
        return web_crawler.fetcher.URLLibFetcher()

    async def test_http_error_raises_fetch_error(self, fetcher):
        """HTTPError (e.g., 404, 500) is converted to FetchError."""
        http_error = urllib.error.HTTPError(
            url="http://example.com/missing",
            code=404,
            msg="Not Found",
            hdrs=unittest.mock.MagicMock(),
            fp=None,
        )
        with unittest.mock.patch.object(
            fetcher._opener, "open", side_effect=http_error
        ):
            with pytest.raises(web_crawler.exceptions.FetchError) as exc_info:
                await fetcher.fetch("http://example.com/missing")

            assert exc_info.value.url == "http://example.com/missing"
            assert "404" in exc_info.value.reason
            assert "Not Found" in exc_info.value.reason

    async def test_http_500_error_raises_fetch_error(self, fetcher):
        """HTTPError with 500 status is converted to FetchError."""
        http_error = urllib.error.HTTPError(
            url="http://example.com/error",
            code=500,
            msg="Internal Server Error",
            hdrs=unittest.mock.MagicMock(),
            fp=None,
        )
        with unittest.mock.patch.object(
            fetcher._opener, "open", side_effect=http_error
        ):
            with pytest.raises(web_crawler.exceptions.FetchError) as exc_info:
                await fetcher.fetch("http://example.com/error")

            assert exc_info.value.url == "http://example.com/error"
            assert "500" in exc_info.value.reason

    async def test_url_error_raises_fetch_error(self, fetcher):
        """URLError (e.g., DNS failure, connection refused) is converted to
        FetchError.
        """
        url_error = urllib.error.URLError(reason="Name or service not known")
        with unittest.mock.patch.object(fetcher._opener, "open", side_effect=url_error):
            with pytest.raises(web_crawler.exceptions.FetchError) as exc_info:
                await fetcher.fetch("http://nonexistent.invalid/page")

            assert exc_info.value.url == "http://nonexistent.invalid/page"
            assert "Name or service not known" in exc_info.value.reason

    async def test_url_error_connection_refused(self, fetcher):
        """URLError from connection refused is converted to FetchError."""
        url_error = urllib.error.URLError(reason="Connection refused")
        with unittest.mock.patch.object(fetcher._opener, "open", side_effect=url_error):
            with pytest.raises(web_crawler.exceptions.FetchError) as exc_info:
                await fetcher.fetch("http://localhost:9999/page")

            assert exc_info.value.url == "http://localhost:9999/page"
            assert "Connection refused" in exc_info.value.reason

    async def test_timeout_error_raises_fetch_error(self, fetcher):
        """TimeoutError is converted to FetchError with timeout message."""
        with unittest.mock.patch.object(
            fetcher._opener, "open", side_effect=TimeoutError()
        ):
            with pytest.raises(web_crawler.exceptions.FetchError) as exc_info:
                await fetcher.fetch("http://slow.example.com/page")

            assert exc_info.value.url == "http://slow.example.com/page"
            assert "timed out" in exc_info.value.reason.lower()

    async def test_successful_fetch_returns_response(self, fetcher):
        """Successful fetch returns FetchResponse with correct fields."""
        mock_response = unittest.mock.MagicMock()
        mock_response.status = 200
        mock_response.geturl.return_value = "http://example.com/page"
        mock_response.getheaders.return_value = [
            ("Content-Type", "text/html"),
            ("Content-Length", "13"),
        ]
        mock_response.read.return_value = b"<html></html>"

        with unittest.mock.patch.object(
            fetcher._opener, "open", return_value=mock_response
        ):
            result = await fetcher.fetch("http://example.com/page")

        assert isinstance(result, web_crawler.fetcher.FetchResponse)
        assert result.status_code == 200
        assert result.headers == {
            "Content-Type": "text/html",
            "Content-Length": "13",
        }
        assert result.body == b"<html></html>"
        assert result.url == "http://example.com/page"

    async def test_fetch_passes_custom_headers(self, fetcher):
        """Custom headers are passed to the request."""
        mock_response = unittest.mock.MagicMock()
        mock_response.status = 200
        mock_response.geturl.return_value = "http://example.com/page"
        mock_response.getheaders.return_value = []
        mock_response.read.return_value = b""

        with unittest.mock.patch.object(
            fetcher._opener, "open", return_value=mock_response
        ) as mock_urlopen:
            await fetcher.fetch(
                "http://example.com",
                headers={"User-Agent": "TestBot/1.0"},
            )

            # Verify urlopen was called with a Request object
            call_args = mock_urlopen.call_args
            request_obj = call_args[0][0]
            assert request_obj.get_header("User-agent") == "TestBot/1.0"

    async def test_fetch_passes_timeout(self, fetcher):
        """Timeout parameter is passed to urlopen."""
        mock_response = unittest.mock.MagicMock()
        mock_response.status = 200
        mock_response.geturl.return_value = "http://example.com/page"
        mock_response.getheaders.return_value = []
        mock_response.read.return_value = b""

        with unittest.mock.patch.object(
            fetcher._opener, "open", return_value=mock_response
        ) as mock_urlopen:
            await fetcher.fetch("http://example.com", timeout=10.0)

            call_args = mock_urlopen.call_args
            assert call_args[1]["timeout"] == pytest.approx(10.0, abs=0.1)
