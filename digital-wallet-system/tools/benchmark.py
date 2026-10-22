import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import platform
import time
from lab import Lab

parser=argparse.ArgumentParser(description="Small real-HTTP benchmark with synthetic funds")
parser.add_argument("--transfers",type=int,default=20)
parser.add_argument("--clients",type=int,default=4)
parser.add_argument("--output",default="benchmarks/latest.json")
args=parser.parse_args()
if not 2<=args.transfers<=80 or not 1<=args.clients<=8:
    parser.error("reference sample supports 2..80 transfers and 1..8 clients")
with Lab() as lab:
    def transfer(index):
        start=time.perf_counter()
        id="bench_"+str(index)
        lab.transfer(id,100,local=index%2==0)
        lab.done(id)
        return (time.perf_counter()-start)*1000
    started=time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        latency=sorted(pool.map(transfer,range(args.transfers)))
    elapsed=time.perf_counter()-started
    audit=lab.audit()
    balances=[lab.balance(account) for account in lab.config["accounts"]]
    assert audit["matches"] and audit["in_transit"]==0 and audit["wallet_total"]==30000
    assert lab.balance()["available"]==10000-args.transfers*100
    result={"python":platform.python_version(),"platform":platform.platform(),"processes":7,"transfers":args.transfers,
            "local_transfers":(args.transfers+1)//2,"cross_shard_transfers":args.transfers//2,"clients":args.clients,
            "elapsed_seconds":elapsed,"completed_transfers_per_second":args.transfers/elapsed,
            "accept_to_terminal_p50_ms":latency[math.ceil(len(latency)*.5)-1],"accept_to_terminal_p95_ms":latency[math.ceil(len(latency)*.95)-1],
            "audit":audit,"balances":balances,"scope":"Single fixed coordinator; 2-of-3 real HTTP journal replicas; two unreplicated SQLite participants; includes terminal-state polling, excludes startup and final audit."}
path=Path(args.output)
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result,indent=2))
