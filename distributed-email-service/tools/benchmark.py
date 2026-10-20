import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import time

from lab import Lab,wait_for


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default="benchmarks/results.json")
    args=parser.parse_args()
    with Lab() as lab:
        start=time.perf_counter()
        def send(i):
            return lab.send(f"bench-{i}",subject=f"Invoice {i}",body=f"searchable project alpha record {i}")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(send,range(40)))
        admission=time.perf_counter()-start
        wait_for(lab.settled,timeout=60)
        wait_for(lab.caught_up,timeout=60)
        delivered=time.perf_counter()-start
        result=lab.api("/v1/messages?q=project+alpha&field=body&replica=true",user=lab.recipient)
        if len(result["messages"])!=40:
            raise AssertionError("replica search missed accepted messages")
        def search(_):
            begin=time.perf_counter()
            value=lab.api("/v1/messages?q=project+alpha&field=body&replica=true",user=lab.recipient)
            if len(value["messages"])!=40:
                raise AssertionError("search result mismatch")
            return (time.perf_counter()-begin)*1000
        begin=time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            latencies=list(pool.map(search,range(40)))
        search_seconds=time.perf_counter()-begin
        spool=lab.call("smtp","/admin/status",user="admin")
        output={"python":platform.python_version(),"platform":platform.platform(),"processes":9,
                "primary_shards":2,"read_replicas":2,"messages":40,"send_clients":4,
                "admission_seconds":round(admission,3),"all_replica_searchable_seconds":round(delivered,3),
                "admitted_messages_per_second":round(40/admission,2),
                "delivered_and_replicated_per_second":round(40/delivered,2),
                "unique_smtp_deliveries":spool["unique_deliveries"],"replica_search_matches":40,
                "search_requests":40,"search_clients":4,"search_p50_ms":round(statistics.median(latencies),3),
                "search_p95_ms":round(sorted(latencies)[37],3),"search_requests_per_second":round(40/search_seconds,2),
                "sqlite_bytes":sum(p.stat().st_size for p in lab.root.glob("*.sqlite*")),
                "object_bytes":sum(p.stat().st_size for p in (lab.root/"objects").glob("*/*")),
                "scope":"real localhost HTTP + SMTP + immutable objects + asynchronous metadata replicas; no external mail, real AV or replicated object storage"}
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(output,indent=2)+"\n")
    print(json.dumps(output,indent=2))


if __name__=="__main__":
    main()
