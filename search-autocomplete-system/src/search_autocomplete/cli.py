"""Completed-search collection, offline workers and bounded HTTP serving."""

import argparse
import json
import sys
import time

from search_autocomplete import Autocomplete, Collector, SnapshotWorker, Store
from search_autocomplete.http import Coordinator, server
from search_autocomplete.worker import WEEK


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="autocomplete.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record")
    record.add_argument("event_id")
    record.add_argument("query")
    record.add_argument("--at", type=float)
    record.add_argument("--sample-every", type=int, default=1)
    for name in ("build", "aggregate"):
        command = commands.add_parser(name)
        command.add_argument("--start", type=float)
        command.add_argument("--end", type=float)
        if name == "build":
            command.add_argument("--shards", type=int, default=1)
    commands.add_parser("aggregation").add_argument("id", type=int)
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--shards", type=int, default=1)
    worker.add_argument("--interval", type=float, default=WEEK)
    worker.add_argument("--window", type=float, default=WEEK)
    worker.add_argument("--retry-delay", type=float, default=5)
    worker.add_argument("--purge-blocked", action="store_true")
    commands.add_parser("query").add_argument("prefix")
    commands.add_parser("purge-blocked")
    for name in ("block", "unblock"):
        commands.add_parser(name).add_argument("query")
    for name in ("node", "coordinator"):
        command = commands.add_parser(name)
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8080)
        if name == "node":
            command.add_argument("--shard", type=int, required=True)
        else:
            command.add_argument(
                "--replicas",
                required=True,
                help="JSON map file: shard ID -> endpoint list",
            )
    return parser


def _emit(value):
    print(json.dumps(value), flush=True)


def _window(args):
    end = time.time() if args.end is None else args.end
    start = max(0, end - WEEK) if args.start is None else args.start
    return start, end


def _serve(store, args):
    coordinator = None
    try:
        if args.command == "coordinator":
            with open(args.replicas, encoding="utf-8") as stream:
                replicas = {int(key): value for key, value in json.load(stream).items()}
            coordinator = Coordinator(store, replicas)
            service, shard = coordinator, None
        else:
            service, shard = Autocomplete(store), args.shard
            if not 0 <= shard < len(store.snapshot()["shards"]):
                raise ValueError("shard is absent from current snapshot")
        http = server(service, args.host, args.port, shard)
        try:
            print("Autocomplete service ready", flush=True)
            http.serve_forever()
        finally:
            http.server_close()
    finally:
        if coordinator is not None:
            coordinator.close()


def _run_worker(store, args):
    worker = SnapshotWorker(
        store,
        shards=args.shards,
        interval=args.interval,
        window=args.window,
        retry_delay=args.retry_delay,
        purge_blocked=args.purge_blocked,
    )
    if args.once:
        _emit({"version": worker.run_once()})
    else:
        worker.run(
            on_publish=lambda version: _emit({"version": version}),
            on_error=lambda error: print(
                f"Build failed; retrying: {error}", file=sys.stderr, flush=True
            ),
        )


def _execute(store, args):
    if args.command == "record":
        collector = Collector(store, args.sample_every)
        _emit(
            {
                "selected": collector.selected(args.event_id),
                "created": collector.record(args.event_id, args.query, args.at),
            }
        )
    elif args.command == "build":
        _emit({"version": store.build(*_window(args), args.shards)})
    elif args.command == "aggregate":
        _emit({"aggregation_id": store.aggregate(*_window(args))})
    elif args.command == "aggregation":
        _emit(store.aggregation(args.id))
    elif args.command == "worker":
        _run_worker(store, args)
    elif args.command in ("block", "unblock"):
        _emit({"revision": store.block(args.query, args.command == "block")})
    elif args.command == "purge-blocked":
        _emit({"deleted": store.purge_blocked()})
    elif args.command == "query":
        _emit(Autocomplete(store).suggest(args.prefix))
    else:
        _serve(store, args)


def main(argv=None):
    args = _parser().parse_args(argv)
    store = Store(args.db)
    try:
        _execute(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
