import argparse
import os
import sys
from pathlib import Path

from .client import DeviceClient
from .http import server
from .service import Drive
from .watcher import SyncWatcher


def _parser():
    parser = argparse.ArgumentParser(
        description="Encrypted file synchronization reference"
    )
    parser.add_argument("--root", default=".runtime/server")
    parser.add_argument("--block-size", type=int, default=4 * 1024**2)
    commands = parser.add_subparsers(dest="command", required=True)
    token = commands.add_parser("token")
    token.add_argument("user")
    api = commands.add_parser("api")
    api.add_argument("--port", type=int, default=8081)
    api.add_argument("--host", default="127.0.0.1")
    for name in ("push", "pull", "delete", "sync"):
        command = commands.add_parser(name)
        command.add_argument("--url", default="http://127.0.0.1:8081")
        command.add_argument("--folder", required=True)
        command.add_argument("--device", required=True)
        command.add_argument("--space", required=name != "pull")
        if name in ("push", "delete"):
            command.add_argument("path")
        if name == "sync":
            command.add_argument("--interval", type=float, default=1)
            command.add_argument("--poll-wait", type=float, default=1)
            command.add_argument("--max-files", type=int, default=10000)
    commands.add_parser("cleanup")
    for name in ("archive", "restore-block"):
        commands.add_parser(name).add_argument("key")
    return parser


def _device(args):
    client = DeviceClient(
        args.url, os.environ["FILE_SYNC_TOKEN"], args.folder, args.device
    )
    try:
        if args.space:
            client.bind_space(args.space)
        if args.command == "pull":
            batch = client.pull()
            print(f"Applied {len(batch['events'])} events")
        elif args.command == "sync":
            SyncWatcher(
                client,
                args.space,
                interval=args.interval,
                poll_wait=args.poll_wait,
                max_files=args.max_files,
            ).run(
                on_error=lambda error: print(
                    f"Sync will retry: {error}", file=sys.stderr, flush=True
                )
            )
        else:
            getattr(client, args.command)(args.space, args.path)
            print("File synchronized")
    finally:
        client.close()


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command in ("push", "pull", "delete", "sync"):
        _device(args)
        return
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    drive = Drive(root / "metadata.sqlite3", root / "objects", args.block_size)
    try:
        if args.command == "token":
            print(drive.issue(args.user))
        elif args.command == "cleanup":
            print(drive.cleanup())
        elif args.command in ("archive", "restore-block"):
            getattr(drive.store, "archive" if args.command == "archive" else "restore")(
                args.key
            )
            print("Local block tier updated")
        else:
            http = server(drive, args.host, args.port)
            print("File sync API ready", flush=True)
            try:
                http.serve_forever()
            finally:
                http.server_close()
    finally:
        drive.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
