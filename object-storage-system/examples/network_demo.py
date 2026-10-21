from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
from lab import Lab
from object_storage.common import dumps,sha


def main():
    with Lab() as lab:
        begin=time.perf_counter()
        first=lab.put("folder/demo.txt",b"version one","demo-one")
        lab.put("folder/demo.txt",b"version two","demo-two")
        old=lab.get("folder/demo.txt",first["version"])
        lab.stop("data-0")
        degraded=lab.get("folder/demo.txt")
        lab.spawn("data-0")
        data=b"multipart-"*100000
        upload=lab.api("/v1/buckets/bucket-test/uploads",{"key":"large","request_key":"demo-multipart","sha256":sha(data),"size":len(data)})["id"]
        for number,offset in enumerate(range(0,len(data),600000),1):
            part=data[offset:offset+600000]
            lab.api(f"/v1/uploads/{upload}/parts/{number}",part,"PUT",headers={"X-SHA256":sha(part)})
        lab.api(f"/v1/uploads/{upload}/complete",{},"POST")
        matches=lab.get("large")==data
        maintenance=lab.api("/admin/maintenance",{"grace":60},"POST",role="admin")
        print(dumps({"processes":6,"metadata_shards":2,"data_replicas":3,"write_quorum":2,
                     "old_version_matches":old==b"version one","read_with_one_node_down":degraded==b"version two",
                     "multipart_bytes":len(data),"multipart_matches":matches,"unrecoverable_chunks":maintenance["unrecoverable"],
                     "demo_ms":round((time.perf_counter()-begin)*1000,2)}))


if __name__=="__main__":
    main()
