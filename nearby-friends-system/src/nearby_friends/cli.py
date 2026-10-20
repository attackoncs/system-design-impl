import argparse
import asyncio
import json
from pathlib import Path
import secrets

from .backend import Backend
from .core import Store
from .server import run_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command",choices=["serve","provision","ring"])
    parser.add_argument("--db",default=".runtime/nearby.sqlite")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8090)
    parser.add_argument("--cache-url",default="redis://127.0.0.1:6379/0")
    parser.add_argument("--namespace",default="nearby")
    parser.add_argument("--ttl",type=float,default=600)
    parser.add_argument("--retention",type=float,default=86400)
    parser.add_argument("--reconcile",type=float,default=1)
    parser.add_argument("--user")
    parser.add_argument("--admin",action="store_true")
    parser.add_argument("--token-file")
    parser.add_argument("--nodes",help="JSON broker ID -> Redis URL file")
    args = parser.parse_args()
    store = Store(args.db,ttl=args.ttl,retention=args.retention)
    if args.command == "provision":
        if not args.user or not args.token_file:
            parser.error("provision requires --user and --token-file")
        # Exclusive creation prevents replacing an existing private token file.
        token = secrets.token_urlsafe(32)
        with Path(args.token_file).open("x",encoding="utf-8") as output:
            store.provision(args.user,token,args.admin)
            output.write(token)
        print("credential provisioned into private token file")
    elif args.command == "ring":
        if not args.user or not args.nodes:
            parser.error("ring requires local admin --user and --nodes")
        print(json.dumps(store.set_ring(args.user,json.loads(Path(args.nodes).read_text(encoding="utf-8")),store.ring()["epoch"])))
    else:
        backend = Backend(store,args.cache_url,args.namespace)
        try:
            asyncio.run(run_server(backend,args.host,args.port,args.reconcile))
        except KeyboardInterrupt:
            pass
        finally:
            backend.close()


if __name__ == "__main__":
    main()
