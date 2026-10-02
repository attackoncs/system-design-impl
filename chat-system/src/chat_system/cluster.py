"""CLI for chat nodes, discovery, credential provisioning and push workers."""
import argparse
import asyncio
import json
import os
from contextlib import suppress
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

from .distributed import DistributedChatService
from .models import ChatConfig, ChatError
from .push import HTTPWebhookNotifier, PushWorker
from .redis_backend import RedisBackend
from .transport import WebSocketChatServer


class DiscoveryHTTPServer:
    def __init__(self, backend):
        self.backend = backend

    async def handler(self, reader, writer):
        status, body = "400 Bad Request", {"error": "invalid request"}
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            if len(request) > 8192:
                raise ValueError("headers too large")
            line = request.split(b"\r\n", 1)[0].decode("ascii")
            method, target, _ = line.split(" ")
            parsed = urlsplit(target)
            if method == "GET" and parsed.path == "/discover":
                region = parse_qs(parsed.query).get("region", [None])[0]
                server = await self.backend.select(region)
                status, body = "200 OK", asdict(server)
            elif method == "GET" and parsed.path == "/health":
                await self.backend.redis.ping()
                status, body = "200 OK", {"ok": True}
            else:
                status, body = "404 Not Found", {"error": "not found"}
        except (ChatError, ConnectionError):
            status, body = "503 Service Unavailable", {"error": "no available chat server"}
        except (ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:
            status, body = "503 Service Unavailable", {"error": "service unavailable"}
        try:
            payload = json.dumps(body).encode()
            writer.write((f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def start(self, host="127.0.0.1", port=8080):
        return await asyncio.start_server(self.handler, host, port, limit=8192)


async def run(args):
    backend = make_backend(args)
    service = None
    try:
        if args.command == "token":
            print(await backend.issue(args.user), flush=True)
        elif args.command == "revoke":
            token = os.environ.get("CHAT_TOKEN")
            if not token:
                raise ValueError("set CHAT_TOKEN for revocation")
            await backend.revoke(token)
        elif args.command == "discover":
            print(json.dumps(asdict(await backend.select(args.region))), flush=True)
        elif args.command == "discovery":
            server = await DiscoveryHTTPServer(backend).start(args.host, args.port)
            async with server:
                print("Discovery service ready", flush=True)
                await server.serve_forever()
        elif args.command == "node":
            service = DistributedChatService(backend, args.node_id, args.public_url, args.region,
                                              args.capacity, args.lease, args.poll,
                                              ChatConfig(heartbeat_timeout=args.heartbeat_timeout))
            adapter = WebSocketChatServer(service)
            # Open socket before publishing the node's lease.
            async with adapter.running(args.host, args.port):
                await service.start()
                print("Chat node ready", flush=True)
                await asyncio.Future()
        elif args.command == "worker":
            url = os.environ.get("CHAT_PUSH_URL")
            notifier = HTTPWebhookNotifier(url, os.environ.get("CHAT_PUSH_TOKEN")) if url else None
            print("Push worker ready", flush=True)
            await PushWorker(backend, notifier, reclaim_idle_ms=args.reclaim_ms,
                             retry_base=args.retry_base, max_attempts=args.max_attempts).run()
    finally:
        if service is not None:
            await service.close()
        await backend.close()


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--redis-url", default=os.environ.get("CHAT_REDIS_URL", "redis://127.0.0.1:6379/0"))
    root.add_argument("--namespace", default=os.environ.get("CHAT_NAMESPACE", "chat"))
    root.add_argument("--sentinels", default=os.environ.get("CHAT_REDIS_SENTINELS", ""),
                      help="comma separated host:port endpoints")
    root.add_argument("--master-name", default=os.environ.get("CHAT_REDIS_MASTER", "chat-primary"))
    root.add_argument("--wait-replicas", type=int, default=None)
    root.add_argument("--wait-timeout-ms", type=int, default=1000)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("token").add_argument("user")
    commands.add_parser("revoke")
    select = commands.add_parser("discover")
    select.add_argument("--region")
    api = commands.add_parser("discovery")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8080)
    node = commands.add_parser("node")
    node.add_argument("--node-id", required=True)
    node.add_argument("--public-url", required=True)
    node.add_argument("--host", default="127.0.0.1")
    node.add_argument("--port", type=int, default=8765)
    node.add_argument("--region", default="local")
    node.add_argument("--capacity", type=int, default=1000)
    node.add_argument("--lease", type=float, default=10)
    node.add_argument("--poll", type=float, default=0.2)
    node.add_argument("--heartbeat-timeout", type=float, default=30)
    worker = commands.add_parser("worker")
    worker.add_argument("--reclaim-ms", type=int, default=15000)
    worker.add_argument("--retry-base", type=float, default=1)
    worker.add_argument("--max-attempts", type=int, default=5)
    return root


def make_backend(args):
    replicas = args.wait_replicas
    if args.sentinels:
        endpoints = []
        for value in args.sentinels.split(","):
            host, port = value.strip().rsplit(":", 1)
            endpoints.append((host, int(port)))
        return RedisBackend.from_sentinel(endpoints, args.master_name, args.namespace,
            password=os.environ.get("CHAT_REDIS_PASSWORD"),
            sentinel_password=os.environ.get("CHAT_SENTINEL_PASSWORD"),
            wait_replicas=1 if replicas is None else replicas, wait_timeout_ms=args.wait_timeout_ms)
    return RedisBackend.from_url(args.redis_url, args.namespace,
        wait_replicas=0 if replicas is None else replicas, wait_timeout_ms=args.wait_timeout_ms)


def main():
    try:
        asyncio.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
