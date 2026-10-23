import argparse
import json
from pathlib import Path

from .http import Replicator, Router, server
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["primary", "replica", "gateway", "publish"])
    parser.add_argument("--db", default=".runtime/primary.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tokens", help="JSON owners/admin/replication credentials")
    parser.add_argument("--upstream")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--regions", help="JSON region -> list of replica URLs")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    tokens = (
        json.loads(Path(args.tokens).read_text(encoding="utf-8")) if args.tokens else {}
    )
    if args.role == "gateway":
        if not args.regions:
            parser.error("gateway requires --regions")
        httpd = server(
            args.host,
            args.port,
            router=Router(json.loads(Path(args.regions).read_text(encoding="utf-8"))),
        )
        replicator = None
    else:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        store = Store(args.db, shards=args.shards, readonly=args.role == "replica")
        if args.role == "publish":
            print(json.dumps(store.publish(force=args.force)))
            return
        replicator = None
        if args.role == "replica":
            if not args.upstream or not tokens.get("replication") or args.interval <= 0:
                parser.error(
                    "replica requires --upstream, replication token and positive interval"
                )
            replicator = Replicator(
                store, args.upstream, tokens["replication"], args.interval
            )
            replicator.thread.start()
        httpd = server(
            args.host, args.port, store=store, tokens=tokens, replicator=replicator
        )
    print(
        f"{args.role} listening on http://{args.host}:{httpd.server_port}", flush=True
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if replicator:
            replicator.stop.set()
            replicator.thread.join(timeout=5)


if __name__ == "__main__":
    main()
