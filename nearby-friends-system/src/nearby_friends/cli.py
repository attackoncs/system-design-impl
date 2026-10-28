import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets

from .backend import Backend
from .core import Store
from .server import run_server


def provision_token(store, user, admin, path):
    """Persist the credential before accepting the user in the local database."""
    token = secrets.token_urlsafe(32)
    target = Path(path)
    created = None
    try:
        # Exclusive creation protects an existing credential from replacement.
        with open(
            target,
            "x",
            encoding="utf-8",
            opener=lambda name, flags: os.open(name, flags, 0o600),
        ) as output:
            created = os.fstat(output.fileno())
            output.write(token)
            output.flush()
            os.fsync(output.fileno())
        store.provision(user, token, admin)
    except BaseException:
        if created is not None:
            try:
                current = target.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    target.unlink()
            except OSError:
                # Keep the provisioning failure visible if cleanup is unavailable.
                pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "provision", "ring"])
    parser.add_argument("--db", default=".runtime/nearby.sqlite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--cache-url", default="redis://127.0.0.1:6379/0")
    parser.add_argument("--namespace", default="nearby")
    parser.add_argument("--ttl", type=float, default=600)
    parser.add_argument("--retention", type=float, default=86400)
    parser.add_argument("--reconcile", type=float, default=1)
    parser.add_argument("--max-connections", type=int, default=256)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--send-timeout", type=float, default=3)
    parser.add_argument("--user")
    parser.add_argument("--admin", action="store_true")
    parser.add_argument("--token-file")
    parser.add_argument("--nodes", help="JSON broker ID -> Redis URL file")
    args = parser.parse_args(argv)
    store = Store(args.db, ttl=args.ttl, retention=args.retention)
    if args.command == "provision":
        if not args.user or not args.token_file:
            parser.error("provision requires --user and --token-file")
        provision_token(store, args.user, args.admin, args.token_file)
        print("credential provisioned into private token file")
    elif args.command == "ring":
        if not args.user or not args.nodes:
            parser.error("ring requires local admin --user and --nodes")
        print(
            json.dumps(
                store.set_ring(
                    args.user,
                    json.loads(Path(args.nodes).read_text(encoding="utf-8")),
                    store.ring()["epoch"],
                )
            )
        )
    else:
        backend = Backend(store, args.cache_url, args.namespace)
        try:
            asyncio.run(
                run_server(
                    backend,
                    args.host,
                    args.port,
                    args.reconcile,
                    max_connections=args.max_connections,
                    workers=args.workers,
                    send_timeout=args.send_timeout,
                )
            )
        except KeyboardInterrupt:
            pass
        finally:
            backend.close()


if __name__ == "__main__":
    main()
