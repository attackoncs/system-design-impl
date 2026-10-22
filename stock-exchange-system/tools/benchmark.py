import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import platform
import time
from lab import Lab

parser=argparse.ArgumentParser(description="Synthetic real-HTTP limit-order round-trip benchmark")
parser.add_argument("--pairs",type=int,default=20)
parser.add_argument("--clients",type=int,default=4)
parser.add_argument("--output",default="benchmarks/latest.json")
args=parser.parse_args()
if not 2<=args.pairs<=80 or not 1<=args.clients<=8:
    parser.error("reference sample supports 2..80 pairs and 1..8 clients")
with Lab() as lab:
    for index in range(args.pairs):
        lab.order("ask_"+str(index),"SELL",100,1,"bob")
    lab.caught_up()
    def take(index):
        start=time.perf_counter()
        id="take_"+str(index)
        lab.order(id,"BUY",101,1)
        result=lab.result(id)
        assert result["accepted"] and result["status"]=="FILLED"
        return (time.perf_counter()-start)*1000
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        latency=sorted(pool.map(take,range(args.pairs)))
    elapsed=time.perf_counter()-started
    lab.caught_up()
    lab.market_ready()
    primary,backup=lab.raw(),lab.raw("backup")
    assert primary["state_hash"]==backup["state_hash"]
    assert len(primary["state"]["trades"])==args.pairs
    audit=lab.call("/admin/audit",role="primary",auth="admin")
    assert audit["matches"] and audit["projection_matches"]
    result={"python":platform.python_version(),"platform":platform.platform(),"processes":4,"resting_orders":args.pairs,"measured_taker_orders":args.pairs,"clients":args.clients,
            "elapsed_seconds":elapsed,"filled_taker_orders_per_second":args.pairs/elapsed,
            "submit_to_fill_p50_ms":latency[math.ceil(len(latency)*.5)-1],"submit_to_fill_p95_ms":latency[math.ceil(len(latency)*.95)-1],"submit_to_fill_p99_ms":latency[math.ceil(len(latency)*.99)-1],
            "trades":len(primary["state"]["trades"]),"replicas_match":True,"audit":audit,
            "scope":"Loopback HTTP, sequencer SQLite FULL writes and asynchronous matching replicas, preseeded resting asks; includes client polling, excludes setup, replica catch-up and final market/audit drain."}
path=Path(args.output)
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result,indent=2))
