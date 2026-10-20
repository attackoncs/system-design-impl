import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from lab import Client, Cluster


async def main():
    cluster = Cluster()
    clients = []
    try:
        cluster.start_redis()
        urls = [await cluster.start_node(name) for name in ["ws-a","ws-b"]]
        alice = await Client.open(urls[0],cluster.tokens["alice"])
        bob = await Client.open(urls[1],cluster.tokens["bob"])
        clients = [alice,bob]
        await alice.command("sharing",enabled=True)
        await bob.command("sharing",enabled=True)
        await alice.command("friend.request",target="bob")
        await bob.command("friend.accept",target="alice")
        await alice.command("location",latitude=0,longitude=179.999,timestamp=time.time())
        started = time.perf_counter()
        ack = await bob.command("location",latitude=0,longitude=-179.999,timestamp=time.time())
        def entered(message):
            return message.get("type") == "delta" and any(c["event"] == "enter" and c["friend"]["id"] == "bob" for c in message["changes"])
        await alice.until(entered)
        elapsed = (time.perf_counter()-started)*1000
        snapshot = await alice.command("snapshot")
        await bob.command("sharing",enabled=False)
        after = await alice.command("snapshot")
        print(json.dumps({"websocket_processes":2,"redis_processes":2,"distance_m":snapshot["friends"][0]["distance_m"],"cross_node_entry_ms":round(elapsed,3),"hint_published":ack["hint_published"],"after_sharing_disabled":after["total"]},indent=2))
    finally:
        for client in clients:
            await client.close()
        cluster.close()


if __name__ == "__main__":
    asyncio.run(main())
