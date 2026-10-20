import argparse
import ipaddress
import json
from pathlib import Path
import threading

from .common import identifier,integer
from .http import server
from .objects import ObjectStore
from .smtp import Spool,smtp_server
from .store import MailStore
from .worker import replicate,run_sender


def load_config(path):
    config=json.loads(Path(path).read_text())
    tokens=[config[r+"_token"] for r in ("admin","internal","smtp")]
    if not isinstance(config["users"],dict) or not config["users"]:
        raise ValueError("guest mailbox identities required")
    for user,token in config["users"].items():
        identifier(user)
        tokens.append(token)
    if any(not isinstance(t,str) or len(t)<24 for t in tokens) or len(tokens)!=len(set(tokens)):
        raise ValueError("distinct credentials of at least 24 characters required")
    if not 1<=len(config["primary_urls"])<=32 or len(config["primary_urls"])!=len(config["replica_urls"]):
        raise ValueError("one read replica URL required per primary")
    if len(set(config["primary_urls"]+config["replica_urls"]))!=2*len(config["primary_urls"]):
        raise ValueError("node URLs must be distinct")
    integer(config["smtp_port"],"smtp_port",1024,65535)
    if config.get("scanner","quarantine-all") not in ("test-markers","quarantine-all"):
        raise ValueError("unsupported scanner; no real scanner adapter configured")
    return config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("role",choices=["api","primary","replica","objects","smtp","worker"])
    parser.add_argument("--config",required=True)
    parser.add_argument("--root",default=".runtime")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=9840)
    parser.add_argument("--shard",type=int,default=0)
    args=parser.parse_args()
    config=load_config(args.config)
    integer(args.shard,"shard",0,len(config["primary_urls"])-1)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("this local SMTP reference only binds loopback addresses")
    if args.role=="worker":
        run_sender(config,args.shard)
        return
    root,store,state=Path(args.root),None,{}
    stop=threading.Event()
    smtp=None
    if args.role in ("primary","replica"):
        store=MailStore(root/f"{args.role}-{args.shard}.sqlite",args.shard,len(config["primary_urls"]),args.role=="replica")
        if args.role=="replica":
            threading.Thread(target=replicate,args=(config,store,stop,state),daemon=True).start()
    if args.role=="objects":
        store=ObjectStore(root/"objects")
    if args.role=="smtp":
        store=Spool(root/"smtp-spool.sqlite")
        smtp=smtp_server(config,store,args.host,config["smtp_port"])
        threading.Thread(target=smtp.serve_forever,daemon=True).start()
        def receive_loop():
            while not stop.is_set():
                try:
                    busy=store.process_once(config)
                except Exception:
                    busy=False
                stop.wait(.02 if busy else .1)
        threading.Thread(target=receive_loop,daemon=True).start()
    instance=server(args.role,config,store,args.host,args.port,state)
    try:
        instance.serve_forever(poll_interval=.1)
    finally:
        stop.set()
        instance.server_close()
        if smtp:
            smtp.shutdown()
            smtp.server_close()


if __name__=="__main__":
    main()
