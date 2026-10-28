import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path

from .artifacts import TileArchive, precompute
from .credentials import provision_token
from .http import Gateway, Replicator, server
from .maintenance import Maintenance
from .store import Store
from .tracking import NavigationTracker


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["import", "provision", "precompute", "serve", "replica", "gateway"],
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
    parser.add_argument(
        "--tile-archive", help="Persistent precomputed tile SQLite file"
    )
    parser.add_argument(
        "--tiles", help="JSON list of explicit [z,x,y] tile coordinates"
    )
    parser.add_argument("--max-tiles", type=int, default=256)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--navigation-interval", type=float, default=1)
    parser.add_argument("--max-navigation-sessions", type=int, default=128)
    args = parser.parse_args(argv)
    replicator = tracker = maintenance = None
    with ExitStack() as resources:
        if args.command == "gateway":
            if not args.regions:
                parser.error("gateway requires --regions JSON")
            httpd = server(
                args.host,
                args.port,
                gateway=Gateway(
                    json.loads(Path(args.regions).read_text(encoding="utf-8"))
                ),
                concurrency=args.concurrency,
                request_timeout=args.request_timeout,
            )
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
                provision_token(store, args.user, args.admin, args.token_file)
                print("credential provisioned into private token file")
                return
            if args.command == "precompute":
                if not args.tile_archive or not args.tiles:
                    parser.error("precompute requires --tile-archive and --tiles")
                result = precompute(
                    store,
                    TileArchive(args.tile_archive),
                    json.loads(Path(args.tiles).read_text(encoding="utf-8")),
                    max_tiles=args.max_tiles,
                )
                print(json.dumps(result))
                return
            if args.tile_archive:
                archive_path = Path(args.tile_archive)
                if archive_path.resolve() == Path(args.db).resolve() or (
                    archive_path.exists() and os.path.samefile(archive_path, args.db)
                ):
                    parser.error("tile archive must be separate from private catalog")
                store.tile_archive = TileArchive(args.tile_archive)
            if args.command == "replica":
                if not args.upstream or not args.token_file:
                    parser.error("replica requires --upstream and --token-file")
                replicator = Replicator(
                    store,
                    args.upstream,
                    Path(args.token_file).read_text(encoding="utf-8").strip(),
                    args.interval,
                )
                resources.callback(replicator.close)
            else:
                tracker = NavigationTracker(
                    store,
                    interval=args.navigation_interval,
                    max_sessions=args.max_navigation_sessions,
                )
                resources.callback(tracker.close)
                maintenance = Maintenance(store.prune)
                resources.callback(maintenance.close)
            httpd = server(
                args.host,
                args.port,
                store=store,
                replicator=replicator,
                tracker=tracker,
                concurrency=args.concurrency,
                request_timeout=args.request_timeout,
            )
        # Close HTTP first, then drain helper work before releasing its resources.
        resources.callback(httpd.server_close)
        for worker in (replicator, tracker, maintenance):
            if worker is not None:
                worker.start()
        print(
            f"{args.command} listening on http://{args.host}:{httpd.server_port}",
            flush=True,
        )
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
