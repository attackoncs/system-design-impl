"""Collection, batch building, block rules, query nodes and coordinator commands."""
import argparse
import json
import time

from .http import Coordinator, server
from .service import Autocomplete
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="autocomplete.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("event_id")
    record.add_argument("query")
    record.add_argument("--at", type=float)
    build = commands.add_parser("build")
    build.add_argument("--start", type=float)
    build.add_argument("--end", type=float)
    build.add_argument("--shards", type=int, default=1)
    query = commands.add_parser("query")
    query.add_argument("prefix")
    for name in ("block", "unblock"):
        commands.add_parser(name).add_argument("query")
    for name in ("node", "coordinator"):
        command = commands.add_parser(name)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8080)
        if name == "node":
            command.add_argument("--shard", type=int, required=True)
        else:
            command.add_argument("--replicas", required=True, help="JSON map file: shard ID -> endpoint list")
    args = parser.parse_args()
    store = Store(args.db)
    coordinator = None
    try:
        if args.command == "record":
            print(json.dumps({"created": store.record(args.event_id, args.query, args.at)}))
        elif args.command == "build":
            end = time.time() if args.end is None else args.end
            start = max(0, end - 7 * 86400) if args.start is None else args.start
            print(json.dumps({"version": store.build(start, end, args.shards)}))
        elif args.command in ("block", "unblock"):
            print(json.dumps({"revision": store.block(args.query, args.command == "block")}))
        elif args.command == "query":
            print(json.dumps(Autocomplete(store).suggest(args.prefix)))
        else:
            if args.command == "coordinator":
                with open(args.replicas, encoding="utf-8") as stream:
                    replicas = {int(key): value for key, value in json.load(stream).items()}
                coordinator = Coordinator(store, replicas)
                service = coordinator
            else:
                service = Autocomplete(store)
                snapshot = store.snapshot()
                if not 0 <= args.shard < len(snapshot["shards"]):
                    raise ValueError("shard is absent from current snapshot")
            http = server(service, args.host, args.port, args.shard if args.command == "node" else None)
            print("Autocomplete service ready", flush=True)
            try:
                http.serve_forever()
            finally:
                http.server_close()
    finally:
        if coordinator:
            coordinator.close()
        store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
