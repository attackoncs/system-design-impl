import argparse
import json
from pathlib import Path
import secrets
import threading

from .http import Gateway, Replicator, server
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["import", "provision", "serve", "replica", "gateway"]
    )
    parser.add_argument("--db", default=".runtime/maps.sqlite")
    parser.add_argument("--graph")
    parser.add_argument("--user")
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--token-file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--upstream")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--regions")
    args = parser.parse_args()
    replicator = None
    stop = threading.Event()
    if args.command == "gateway":
        if not args.regions:
            parser.error("gateway requires --regions JSON")
        httpd = server(
            args.host,
            args.port,
            gateway=Gateway(json.loads(Path(args.regions).read_text(encoding="utf-8"))),
        )
        store = None
    else:
        store = Store(args.db, readonly=args.command == "replica")
        if args.command == "import":
            if not args.graph:
                parser.error("import requires --graph")
            print(
                json.dumps(
                    store.import_graph(
                        json.loads(Path(args.graph).read_text(encoding="utf-8"))
                    )
                )
            )
            return
        if args.command == "provision":
            if not args.user or not args.token_file:
                parser.error("provision requires --user and --token-file")
            token = secrets.token_urlsafe(32)
            with Path(args.token_file).open("x", encoding="utf-8") as output:
                store.provision(args.user, token, args.admin)
                output.write(token)
            print("credential provisioned into private token file")
            return
        if args.command == "replica":
            if not args.upstream or not args.token_file or not args.interval > 0:
                parser.error(
                    "replica requires --upstream, --token-file and positive interval"
                )
            replicator = Replicator(
                store,
                args.upstream,
                Path(args.token_file).read_text(encoding="utf-8").strip(),
                args.interval,
            )
            replicator.thread.start()
        httpd = server(args.host, args.port, store=store, replicator=replicator)

    def maintenance():
        while not stop.wait(30):
            if store:
                store.prune()

    worker = threading.Thread(target=maintenance, daemon=True)
    worker.start()
    print(
        f"{args.command} listening on http://{args.host}:{httpd.server_port}",
        flush=True,
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=2)
        httpd.server_close()
        if replicator:
            replicator.stop.set()
            replicator.thread.join(timeout=5)


if __name__ == "__main__":
    main()
