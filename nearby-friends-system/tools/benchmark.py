import argparse
import asyncio
import json
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from lab import Client, Cluster


async def benchmark(args):
    cluster = Cluster()
    clients = []
    try:
        cluster.start_redis()
        urls = [await cluster.start_node(name) for name in ["ws-a","ws-b"]]
        cluster.store.setting("bob","sharing",True)
        cluster.store.update("bob",{"latitude":0,"longitude":0,"timestamp":time.time()})
        for i in range(args.connections):
            user = "viewer"+str(i)
            token = user+"-demo-token"
            cluster.store.provision(user,token)
            cluster.store.setting(user,"sharing",True)
            cluster.store.friendship(user,"bob","friend.request")
            cluster.store.friendship("bob",user,"friend.accept")
            cluster.store.update(user,{"latitude":0,"longitude":0,"timestamp":time.time()})
            client = await Client.open(urls[i%2],token)
            assert client.initial["total"] == 1
            clients.append(client)
        publisher = await Client.open(urls[1],cluster.tokens["bob"])
        clients.append(publisher)
        await asyncio.sleep(0.8)
        samples = []
        deliveries = 0
        for iteration in range(args.updates):
            started = time.perf_counter()
            ack = await publisher.command("location",latitude=0,longitude=(iteration+1)*0.000001,timestamp=time.time())
            revision = ack["revision"]
            def matches(message):
                return message.get("type") == "delta" and any(c.get("friend",{}).get("id") == "bob" and c["friend"]["revision"] == revision for c in message["changes"])
            async def receive(client):
                await client.until(matches,timeout=10)
                return (time.perf_counter()-started)*1000
            elapsed = await asyncio.gather(*(receive(client) for client in clients[:-1]))
            deliveries += len(elapsed)
            samples.extend(elapsed)
        ordered = sorted(samples)
        report = {"python":platform.python_version(),"platform":platform.platform(),"websocket_processes":2,"redis_processes":2,"observer_connections":args.connections,"total_connections":len(clients),"updates":args.updates,"expected_deliveries":args.connections*args.updates,"actual_deliveries":deliveries,"latency_p50_ms":round(statistics.median(samples),3),"latency_p95_ms":round(ordered[int(0.95*(len(ordered)-1))],3),"latency_max_ms":round(max(samples),3),"scope":"localhost Windows WebSocket clients/servers plus WSL Redis; warm connections; end-to-end command through observed delta, not production throughput"}
        output = Path(args.output)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report,indent=2))
    finally:
        for client in clients:
            await client.close()
        cluster.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connections",type=int,default=20)
    parser.add_argument("--updates",type=int,default=10)
    parser.add_argument("--output",default=".runtime/fanout.json")
    args = parser.parse_args()
    if not 1 <= args.connections <= 100 or not 1 <= args.updates <= 100:
        parser.error("connections/updates must be 1..100")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
