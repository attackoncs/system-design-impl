import argparse
import ipaddress
import json
from pathlib import Path

from .common import identifier,integer
from .http import server
from .metadata import Metadata
from .node import DataNode
from .service import Catalog,Service


def load_config(path):
    config=json.loads(Path(path).read_text())
    tokens=[config[k] for k in ("admin_token","internal_token","continuation_secret")]
    if not isinstance(config["users"],dict) or not config["users"]:
        raise ValueError("users required")
    for user,token in config["users"].items():
        identifier(user)
        tokens.append(token)
    if any(not isinstance(t,str) or len(t)<24 for t in tokens) or len(tokens)!=len(set(tokens)):
        raise ValueError("distinct credentials of at least 24 characters required")
    if len(config["data_urls"])!=3 or len(set(config["data_urls"]))!=3:
        raise ValueError("three distinct data node URLs required")
    if not 1<=len(config["metadata_urls"])<=32 or len(set(config["metadata_urls"]))!=len(config["metadata_urls"]):
        raise ValueError("distinct metadata shard URLs required")
    integer(config.get("write_quorum",2),"write_quorum",2,3)
    return config


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("role",choices=["api","metadata","data"])
    parser.add_argument("--config",required=True)
    parser.add_argument("--root",default=".runtime")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=9850)
    parser.add_argument("--index",type=int,default=0)
    args=parser.parse_args()
    config=load_config(args.config)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("reference lab binds loopback only")
    root=Path(args.root)
    if args.role=="api":
        store=Service(config,Catalog(root/"catalog.sqlite"))
    elif args.role=="metadata":
        integer(args.index,"metadata index",0,len(config["metadata_urls"])-1)
        store=Metadata(root/f"metadata-{args.index}.sqlite",args.index,len(config["metadata_urls"]))
    else:
        integer(args.index,"data index",0,2)
        store=DataNode(root/f"data-{args.index}")
    instance=server(args.role,config,store,args.host,args.port)
    try:
        instance.serve_forever(poll_interval=.1)
    finally:
        instance.server_close()


if __name__=="__main__":
    main()
