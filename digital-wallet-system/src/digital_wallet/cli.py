import argparse
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from .common import identifier,integer,request
from .coordinator import Coordinator
from .http import server
from .journal import Replica
from .shard import Shard


def load_config(path):
    config=json.loads(Path(path).read_text())
    urls=[config["coordinator"]]+config["shards"]+config["replicas"]
    if len(config["shards"])!=2 or len(config["replicas"])!=3 or len(set(urls))!=len(urls):
        raise ValueError("two shards, three replicas and distinct loopback endpoints required")
    for value in urls:
        parsed=urlsplit(value)
        if parsed.scheme!="http" or not ipaddress.ip_address(parsed.hostname).is_loopback or parsed.username or parsed.path not in ("","/") or not parsed.port:
            raise ValueError("only loopback HTTP reference endpoints supported")
    tokens=[config["admin_token"],config["internal_token"],*config["owners"].values()]
    if any(not isinstance(value,str) or len(value)<24 for value in tokens) or len(tokens)!=len(set(tokens)):
        raise ValueError("distinct generated role credentials required")
    integer(config["epoch"],1)
    identifier(config["leader"])
    if not 1<=len(config["accounts"])<=100:
        raise ValueError("reference supports 1..100 preprovisioned accounts")
    for account,value in config["accounts"].items():
        identifier(account)
        integer(value["opening"],0,100_000_000)
        if value["owner"] not in config["owners"]:
            raise ValueError("unknown account owner")
    return config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("role",choices=["coordinator","worker","shard","replica"])
    parser.add_argument("--index",type=int,default=0)
    parser.add_argument("--config",required=True)
    parser.add_argument("--root",default=".runtime")
    args=parser.parse_args()
    config=load_config(args.config)
    root=Path(args.root)
    if args.role=="worker":
        while True:
            try:
                request(config["coordinator"]+"/internal/step",config["internal_token"],{})
            except Exception:
                time.sleep(.15)
            time.sleep(.02)
    if args.role=="coordinator":
        store=Coordinator(root/"coordinator.sqlite",config)
        url=config["coordinator"]
    elif args.role=="shard":
        if args.index not in (0,1):
            parser.error("shard index must be 0 or 1")
        store=Shard(root/("shard-"+str(args.index)+".sqlite"),args.index,2,{key:value["opening"] for key,value in config["accounts"].items()},config["epoch"],config["leader"])
        url=config["shards"][args.index]
    else:
        if args.index not in (0,1,2):
            parser.error("replica index must be 0, 1 or 2")
        store=Replica(root/("replica-"+str(args.index)+".sqlite"),config["epoch"],config["leader"])
        url=config["replicas"][args.index]
    parsed=urlsplit(url)
    instance=server(args.role,store,config,parsed.hostname,parsed.port)
    try:
        instance.serve_forever(poll_interval=.05)
    finally:
        instance.server_close()


if __name__=="__main__":
    main()
