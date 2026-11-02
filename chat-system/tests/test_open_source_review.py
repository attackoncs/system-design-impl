"""Endpoint/resource and durable-cursor boundaries found in the chapter audit."""

import asyncio
import io
import json
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from chat_system.client import ReconnectingChatClient
from chat_system.models import ChatError, Message
from chat_system.push import HTTPWebhookNotifier


@pytest.mark.parametrize("code", [302, 401, 503])
@pytest.mark.parametrize("operation", ["discovery", "push"])
def test_http_failure_response_is_closed_without_hiding_the_error(
    monkeypatch, code, operation
):
    body = io.BytesIO(b"failure body")
    error = urllib.error.HTTPError("http://owned.test/", code, "failure", {}, body)

    def fail(*args, **kwargs):
        raise error

    if operation == "discovery":
        monkeypatch.setattr(urllib.request, "urlopen", fail)
        call = ReconnectingChatClient("http://owned.test/", "token", "phone").discover()
    else:
        monkeypatch.setattr(
            urllib.request, "build_opener", lambda *args: SimpleNamespace(open=fail)
        )
        provider = HTTPWebhookNotifier("http://owned.test/")
        call = provider.deliver(
            "bob", Message(1, "room", "alice", "hello", 0.0, "key"), "key"
        )
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            asyncio.run(call)
        assert caught.value.code == code
        assert body.closed
    finally:
        error.close()


BAD_PATHS = [
    "example.org:invalid/chat",
    "example.org:65536/chat",
    "exa mple.org/chat",
    "example.org/\nchat",
    "example.org/\\chat",
    "example.org/\ud800chat",
]


@pytest.mark.parametrize("path", BAD_PATHS)
@pytest.mark.parametrize("target", ["client", "provider"])
def test_invalid_endpoint_configuration_fails_before_network_io(path, target):
    with pytest.raises(ValueError):
        if target == "client":
            ReconnectingChatClient("http://" + path, "token", "phone")
        else:
            HTTPWebhookNotifier("http://" + path)


@pytest.mark.parametrize("path", BAD_PATHS)
def test_invalid_discovered_websocket_endpoint_is_a_protocol_error(monkeypatch, path):
    body = io.BytesIO(json.dumps({"url": "ws://" + path}).encode("utf-8"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: body)
    client = ReconnectingChatClient("http://owned.test/", "token", "phone")
    with pytest.raises(ChatError, match="invalid discovery response"):
        asyncio.run(client.discover())
    assert body.closed


@pytest.mark.parametrize("path", ["[::1]:8080/chat", "例子.中国/chat"])
def test_valid_ipv6_and_international_endpoints_are_preserved(monkeypatch, path):
    discovery = "http://" + path
    endpoint = "ws://" + path
    assert HTTPWebhookNotifier(discovery).url == discovery
    client = ReconnectingChatClient(discovery, "token", "phone")
    body = io.BytesIO(json.dumps({"url": endpoint}).encode("utf-8"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: body)
    assert asyncio.run(client.discover()) == endpoint
    assert body.closed


def test_oversized_saved_cursor_is_rejected(tmp_path):
    path = tmp_path / "cursor"
    path.write_text(str(2**63), encoding="utf-8")
    with pytest.raises(ValueError, match="saved cursor"):
        ReconnectingChatClient("http://owned.test/", "token", "phone", cursor_file=path)


def test_oversized_received_id_cannot_poison_device_progress(tmp_path, monkeypatch):
    path = tmp_path / "cursor"
    client = ReconnectingChatClient(
        "http://owned.test/", "token", "phone", cursor_file=path
    )

    async def call(*args, **kwargs):
        return {"messages": [{"message_id": 2**63}], "next_cursor": 2**63}

    monkeypatch.setattr(client, "call", call)
    with pytest.raises(ChatError, match="invalid inbox page"):
        asyncio.run(client.receive())
    assert client.cursor == 0 and not path.exists()


def test_largest_sqlite_message_id_can_be_received_and_acknowledged(
    tmp_path, monkeypatch
):
    path = tmp_path / "cursor"
    client = ReconnectingChatClient(
        "http://owned.test/", "token", "phone", cursor_file=path
    )

    async def call(*args, **kwargs):
        return {"messages": [{"message_id": 2**63 - 1}], "next_cursor": 2**63 - 1}

    monkeypatch.setattr(client, "call", call)
    page = asyncio.run(client.receive())
    client.acknowledge(page)
    assert path.read_text(encoding="utf-8") == str(2**63 - 1)
    reopened = ReconnectingChatClient(
        "http://owned.test/", "token", "phone", cursor_file=path
    )
    assert reopened.cursor == 2**63 - 1
