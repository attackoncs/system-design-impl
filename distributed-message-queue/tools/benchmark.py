import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from lab import Cluster


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--batches",type=int,default=12)
    parser.add_argument("--batch-size",type=int,default=10)
    parser.add_argument("--clients",type=int,default=2)
    parser.add_argument("--output",default=".runtime/benchmark.json")
    args=parser.parse_args()
    if not 2<=args.batches<=1000 or not 1<=args.batch_size<=100 or not 1<=args.clients<=16:
        parser.error("batches 2..1000, batch-size 1..100, clients 1..16")
    with Cluster() as cluster:
        client=cluster.client
        client.call("/topic",topic="load",partitions=3,segment_bytes=4096)
        def run(index):
            producer=client.producer("load_"+str(index))
            assigned=list(range(index,args.batches,args.clients))
            samples=[]
            for batch in assigned:
                started=time.perf_counter()
                result=producer.send("load",[{"value":"m"*256,"key":str(batch)} for _ in range(args.batch_size)],partition=batch%3)
                assert result["committed"] and len(result["replicas"])>=2
                samples.append((time.perf_counter()-started)*1000)
            return samples
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.clients) as executor:
            samples=[value for values in executor.map(run,range(args.clients)) for value in values]
        elapsed=time.perf_counter()-started
        fetched=0
        for partition in range(3):
            offset=0
            while True:
                batch=client.call("/fetch",topic="load",partition=partition,offset=offset,limit=1000)
                fetched+=len(batch["records"])
                if batch["next_offset"]==offset:
                    break
                offset=batch["next_offset"]
        total=args.batches*args.batch_size
        assert fetched==total
        broker_bytes=sum(p.stat().st_size for i in range(3) for p in (cluster.root/f"b{i}").rglob("*") if p.is_file())
        segments=sum(1 for i in range(3) for _ in (cluster.root/f"b{i}").rglob("*.log"))
        ordered=sorted(samples)
        report={"python":platform.python_version(),"platform":platform.platform(),"brokers":3,"coordinators":1,"tcp_proxies":3,"partitions":3,"clients":args.clients,"batches":args.batches,"batch_size":args.batch_size,"text_bytes_per_message":256,"messages":total,"fetched":fetched,"seconds":round(elapsed,3),"messages_per_second":round(total/elapsed,2),"batch_ack_p50_ms":round(statistics.median(samples),3),"batch_ack_p95_ms":round(ordered[int(0.95*(len(ordered)-1))],3),"broker_disk_bytes":broker_bytes,"segments_across_replicas":segments,"errors":0,"scope":"localhost HTTP with actual independent broker disks, fsync per record and quorum confirmation; small synthetic reference, not production throughput"}
        output=Path(args.output)
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report,indent=2))


if __name__=="__main__":
    main()
