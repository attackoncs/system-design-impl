"""Measured real-WebSocket load with two child chat nodes and isolated Redis keys.

Run from an installed chat-system environment. Optional --kill-node terminates
the tool's own second node halfway through traffic, reconnects its clients, and
checks durable inbox recovery. This tool never stops an external Redis server.
"""
import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import uuid

from websockets.asyncio.client import connect

from chat_system.cluster import make_backend


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def percentiles(values):
    values = sorted(values)
    return {f"p{p}_ms": round(values[min(len(values) - 1, int((len(values) - 1) * p / 100))] * 1000, 3)
            if values else None for p in (50, 95, 99)}


class Peer:
    def __init__(self, user, token, starts, deliveries):
        self.user, self.token = user, token
        self.starts, self.deliveries = starts, deliveries
        self.pending = {}
        self.received = set()
        self.duplicates = 0
        self.counter = 0
        self.ws = self.reader = None

    async def open(self, url):
        self.ws = await connect(url, max_size=2_000_000, open_timeout=15)
        self.reader = asyncio.create_task(self.read())
        await self.call("login", token=self.token, device_id="load-device")

    async def read(self):
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                if event.get("type") == "response":
                    future = self.pending.get(event.get("request_id"))
                    if future and not future.done():
                        future.set_result(event)
                elif event.get("type") == "message":
                    message = event["message"]
                    mid = message["message_id"]
                    if mid in self.received:
                        self.duplicates += 1
                    else:
                        self.received.add(mid)
                        started = self.starts.get(message["client_message_id"])
                        if started is not None:
                            self.deliveries.append(time.perf_counter() - started)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("load peer disconnected"))

    async def call(self, op, **kwargs):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send(json.dumps({"request_id": request_id, "op": op, **kwargs}))
            response = await asyncio.wait_for(future, 20)
            if not response["ok"]:
                raise RuntimeError(response.get("error", "request rejected"))
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.reader:
            with suppress(Exception):
                await self.reader


async def run(args):
    args.namespace = "bench_" + uuid.uuid4().hex
    backend = make_backend(args)
    children, peers = [], []
    starts, deliveries, latencies, errors = {}, [], [], []
    expected = [set() for _ in range(args.connections)]
    gate = asyncio.Semaphore(args.concurrency)
    retries = 0
    heartbeats = None
    try:
        urls = []
        for index in range(2):
            port = free_port()
            url = f"ws://127.0.0.1:{port}"
            urls.append(url)
            command = [sys.executable, "-m", "chat_system.cluster", "--namespace", args.namespace,
                       "--redis-url", args.redis_url, "--wait-timeout-ms", str(args.wait_timeout_ms)]
            if args.sentinels:
                command += ["--sentinels", args.sentinels, "--master-name", args.master_name]
            if args.wait_replicas is not None:
                command += ["--wait-replicas", str(args.wait_replicas)]
            command += ["node", "--node-id", f"load-{index}", "--public-url", url,
                        "--port", str(port), "--capacity", str(args.connections + 4),
                        "--heartbeat-timeout", "60", "--lease", "15", "--poll", "0.5"]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                          stderr=asyncio.subprocess.PIPE, **options)
            children.append(process)
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            if b"ready" not in line.lower():
                raise RuntimeError("chat child startup failed: " + (await process.stderr.read()).decode())
        print(f"Starting {args.connections} WebSocket clients", flush=True)
        admission_start = time.perf_counter()
        async def admit(index):
            async with gate:
                user = f"user-{index}"
                peer = Peer(user, await backend.issue(user), starts, deliveries)
                peers.append((index, peer))
                await peer.open(urls[index % 2])
        await asyncio.gather(*(admit(i) for i in range(args.connections)))
        peers = [peer for _, peer in sorted(peers)]
        admission_seconds = time.perf_counter() - admission_start
        channels = []
        for i in range(0, args.connections, 2):
            channel = await backend.create_channel(peers[i].user, [peers[i + 1].user], "direct", 2)
            channels.append(channel.channel_id)

        async def keep_alive():
            while True:
                await asyncio.sleep(10)
                for peer in peers:
                    with suppress(Exception):
                        await peer.call("heartbeat")
        heartbeats = asyncio.create_task(keep_alive())

        async def phase(first, last):
            nonlocal retries
            async def sender(pair):
                async with gate:
                    for i in range(first, last):
                        if i % len(channels) != pair:
                            continue
                        key = f"load-{i}"
                        started = time.perf_counter()
                        starts[key] = started
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                result = await peers[pair * 2].call("send", channel_id=channels[pair],
                                    content="x" * args.payload_bytes, client_message_id=key)
                                latencies.append(time.perf_counter() - started)
                                for recipient in (pair * 2, pair * 2 + 1):
                                    expected[recipient].add(result["message_id"])
                                break
                            except Exception as error:
                                if time.monotonic() >= deadline:
                                    errors.append(type(error).__name__)
                                    break
                                retries += 1
                                await asyncio.sleep(.2)
            await asyncio.gather(*(sender(pair) for pair in range(len(channels))))

        traffic_start = time.perf_counter()
        split = args.messages // 2 if args.kill_node else args.messages
        await phase(0, split)
        recovery_seconds = None
        if args.kill_node:
            print("Killing owned chat node; reconnecting its clients", flush=True)
            recovery_start = time.perf_counter()
            children[1].kill()
            await children[1].wait()
            async def reconnect(index):
                async with gate:
                    await peers[index].close()
                    await peers[index].open(urls[0])
            await asyncio.gather(*(reconnect(i) for i in range(1, args.connections, 2)))
            recovery_seconds = time.perf_counter() - recovery_start
            await phase(split, args.messages)
        send_seconds = time.perf_counter() - traffic_start
        deadline = time.monotonic() + args.delivery_timeout
        while time.monotonic() < deadline:
            if all(ids.issubset(peer.received) for peer, ids in zip(peers, expected)):
                break
            await asyncio.sleep(.1)
        live_missing = sum(len(ids - peer.received) for peer, ids in zip(peers, expected))

        async def verify(index):
            async with gate:
                peer, ids = peers[index], expected[index]
                after, durable = 0, set()
                while True:
                    page = await peer.call("sync", after=after, limit=1000)
                    messages = page["messages"]
                    durable.update(m["message_id"] for m in messages)
                    if not messages:
                        break
                    after = page["next_cursor"]
                return len(ids - durable), len(durable - ids)
        verified = await asyncio.gather(*(verify(i) for i in range(args.connections)))
        report = {"connections": len(peers), "requested_messages": args.messages,
            "acknowledged_messages": len(latencies), "payload_bytes": args.payload_bytes,
            "admission_seconds": round(admission_seconds, 3), "send_seconds": round(send_seconds, 3),
            "messages_per_second": round(len(latencies) / send_seconds, 2),
            "send_latency": percentiles(latencies), "live_delivery_latency": percentiles(deliveries),
            "live_missing_deliveries": live_missing, "live_duplicate_deliveries": sum(p.duplicates for p in peers),
            "durable_missing_deliveries": sum(v[0] for v in verified),
            "unexpected_durable_messages": sum(v[1] for v in verified), "errors": errors, "retry_attempts": retries,
            "node_killed": args.kill_node, "reconnect_seconds": round(recovery_seconds, 3) if recovery_seconds else None,
            "replica_confirmation": backend.wait_replicas,
            "environment": {"platform": platform.platform(), "python": platform.python_version(),
                            "logical_cpus": os.cpu_count(), "chat_node_processes": 2}}
        report["passed"] = (not errors and len(latencies) == args.messages and
                            not report["durable_missing_deliveries"] and not report["unexpected_durable_messages"]
                            and (args.kill_node or not live_missing))
        print(json.dumps(report, indent=2), flush=True)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report["passed"]
    finally:
        if heartbeats:
            heartbeats.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeats
        for item in peers:
            peer = item[1] if isinstance(item, tuple) else item
            with suppress(Exception):
                await peer.close()
        for process in children:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 8)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        try:
            keys = [k async for k in backend.redis.scan_iter(match=backend.prefix + "*")]
            for offset in range(0, len(keys), 500):
                await backend.redis.delete(*keys[offset:offset + 500])
        finally:
            await backend.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default=os.environ.get("CHAT_REDIS_URL", "redis://127.0.0.1:6379/0"))
    parser.add_argument("--sentinels", default=os.environ.get("CHAT_REDIS_SENTINELS", ""))
    parser.add_argument("--master-name", default="chat-primary")
    parser.add_argument("--wait-replicas", type=int, default=None)
    parser.add_argument("--wait-timeout-ms", type=int, default=1000)
    parser.add_argument("--connections", type=int, default=1000)
    parser.add_argument("--messages", type=int, default=10000)
    parser.add_argument("--payload-bytes", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--delivery-timeout", type=float, default=10)
    parser.add_argument("--kill-node", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.connections < 2 or args.connections % 2 or args.messages < 1 or args.concurrency < 1 or not 1 <= args.payload_bytes <= 99999:
        parser.error("connections must be positive/even; messages, concurrency and payload must be positive")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
