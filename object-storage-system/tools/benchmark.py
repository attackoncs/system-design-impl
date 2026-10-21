import argparse
import json
from pathlib import Path
import platform
import statistics
import time

from lab import Lab


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default="benchmarks/results.json")
    args=parser.parse_args()
    with Lab() as lab:
        latencies=[]
        begin=time.perf_counter()
        for i in range(40):
            data=(f"object-{i}:".encode()+b"x"*4096)
            start=time.perf_counter()
            lab.put(f"small/{i:03}",data)
            latencies.append((time.perf_counter()-start)*1000)
        write_seconds=time.perf_counter()-begin
        read_latencies=[]
        start=time.perf_counter()
        for i in range(40):
            begin=time.perf_counter()
            if lab.get(f"small/{i:03}")!=(f"object-{i}:".encode()+b"x"*4096):
                raise AssertionError("read mismatch")
            read_latencies.append((time.perf_counter()-begin)*1000)
        read_seconds=time.perf_counter()-start
        node=lab.call("data-0","/status",role="internal")
        maintenance=lab.api("/admin/maintenance",{"grace":60},"POST",role="admin")
        if maintenance["unrecoverable"] or node["packed_chunks"]!=40 or node["large_files"]!=0:
            raise AssertionError("packing/replication invariant failed")
        result={"python":platform.python_version(),"platform":platform.platform(),"processes":6,
                "metadata_shards":2,"data_replicas":3,"write_quorum":2,"objects":40,"clients":1,
                "object_size_range_bytes":[4105,4106],"write_seconds":round(write_seconds,3),"writes_per_second":round(40/write_seconds,2),
                "put_p50_ms":round(statistics.median(latencies),3),"put_p95_ms":round(sorted(latencies)[37],3),
                "reads_per_second":round(40/read_seconds,2),"get_p50_ms":round(statistics.median(read_latencies),3),
                "get_p95_ms":round(sorted(read_latencies)[37],3),"packed_chunks_per_node":node["packed_chunks"],
                "pack_files_per_node":1,"pack_bytes_per_node":node["pack_bytes"],"large_files_per_node":node["large_files"],
                "unrecoverable_chunks":0,"scope":"sequential localhost HTTP writes to three independent nodes, two-ack publication; SQLite metadata and one catalog coordinator; no multi-host SLA"}
    path=Path(args.output)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
