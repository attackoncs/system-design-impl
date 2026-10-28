import argparse
from contextlib import ExitStack
from pathlib import Path

from .broker import Broker
from .coordinator import Coordinator
from .http import server
from .lifecycle import Maintenance
from .transport import endpoint, loads, seconds, secret


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["broker", "coordinator"])
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "--config",
        required=True,
        help="private JSON cluster/client tokens and broker ID/URL map",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--lease-seconds", type=float, default=10)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--maintenance-interval", type=float, default=1)
    args = parser.parse_args(argv)
    try:
        seconds(args.lease_seconds)
        seconds(args.request_timeout)
        seconds(args.maintenance_interval)
        if not 1 <= args.concurrency <= 256 or not 0 <= args.port <= 65535:
            raise ValueError("invalid capacity or port")
        with Path(args.config).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("configuration exceeds 64 KiB")
        config = loads(raw)
        if not isinstance(config, dict) or not secret(config.get("cluster_token")):
            raise ValueError("private cluster credential required")
        if args.role == "coordinator":
            if not secret(config.get("client_token")):
                raise ValueError("private client credential required")
            peers = config.get("brokers")
            if not isinstance(peers, dict) or not 1 <= len(peers) <= 9:
                raise ValueError("1..9 brokers required")
            for value in peers.values():
                endpoint(value)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        parser.error("invalid configuration or runtime limits")
    if args.role == "broker":
        service = Broker(args.root)
        token = config["cluster_token"]
    else:
        service = Coordinator(
            args.root, config["brokers"], config["cluster_token"], args.lease_seconds
        )
        token = config["client_token"]
    with ExitStack() as stack:
        worker = None
        if args.role == "coordinator":
            worker = Maintenance(service.maintenance, args.maintenance_interval)
            stack.callback(worker.close)
        httpd = server(
            args.host,
            args.port,
            service,
            token,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
            maintenance=worker,
        )
        stack.callback(httpd.server_close)
        if worker is not None:
            worker.start()
        print(
            f"{args.role} listening on http://{args.host}:{httpd.server_port}",
            flush=True,
        )
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
