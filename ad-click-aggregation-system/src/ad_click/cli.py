import argparse
from contextlib import ExitStack
from pathlib import Path

from .aggregate import Aggregator
from .common import dumps, integer
from .http import WorkerLoop, server
from .log import EventLog
from .service import reconcile, replay
from .tailer import ingest_file
from .transport import endpoint, loads, seconds, secret


def load_config(path):
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("config exceeds 1 MiB")
    config = loads(raw)
    if not isinstance(config, dict):
        raise ValueError("configuration object required")
    for role in ("write", "read", "internal", "admin"):
        token = config.get(role + "_token")
        if not secret(token) or len(token) < 24:
            raise ValueError(role + "_token must contain at least 24 characters")
    if len({config[r + "_token"] for r in ("write", "read", "internal", "admin")}) != 4:
        raise ValueError("role credentials must be distinct")
    integer(config["partitions"], "partitions", 1, 64)
    if (
        not isinstance(config["worker_urls"], list)
        or any(not isinstance(url, str) for url in config["worker_urls"])
        or len(config["worker_urls"]) != config["partitions"]
        or len(set(config["worker_urls"])) != config["partitions"]
    ):
        raise ValueError("one distinct worker URL required per partition")
    for url in [config["ingress_url"], *config["worker_urls"]]:
        parsed = endpoint(url)
        if parsed.query or parsed.path not in ("", "/"):
            raise ValueError("service URL must be an origin")
    integer(config.get("lateness", 120), "lateness", 0, 86400)
    integer(config.get("max_events", 1_000_000), "max_events", 1, 10_000_000)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=["ingress", "worker", "api", "ingest-file", "reconcile", "rebuild"],
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9820)
    parser.add_argument("--partition", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--source")
    parser.add_argument("--checkpoint")
    parser.add_argument(
        "--output",
        help="new database path for rebuild; never overwrites an existing file",
    )
    args = parser.parse_args(argv)
    try:
        integer(args.port, "port", 0, 65535)
        integer(args.concurrency, "concurrency", 1, 256)
        seconds(args.request_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    config = load_config(args.config)
    root = Path(args.root)
    if args.role == "ingest-file":
        if not args.source or not args.checkpoint:
            parser.error("--source and --checkpoint required")
        print(dumps(ingest_file(config, args.source, args.checkpoint)))
        return
    if args.role == "reconcile":
        result = reconcile(config)
        print(dumps(result))
        if not result["matches"]:
            raise SystemExit(1)
        return
    if args.role == "rebuild":
        if not args.output:
            parser.error("--output required")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the name atomically; a failed rebuild leaves its artifact for inspection.
        with output.open("xb"):
            pass
        store = Aggregator(output, args.partition, config.get("lateness", 120))
        print(dumps(replay(config, store)))
        return
    with ExitStack() as stack:
        loop = None
        store = None
        if args.role == "ingress":
            store = EventLog(
                root / "raw.sqlite",
                config["partitions"],
                config.get("max_events", 1_000_000),
            )
        if args.role == "worker":
            integer(args.partition, "partition", 0, config["partitions"] - 1)
            store = Aggregator(
                root / f"worker-{args.partition}.sqlite",
                args.partition,
                config.get("lateness", 120),
            )
            loop = WorkerLoop(config, store)
            stack.callback(loop.close)
        instance = server(
            args.role,
            config,
            store,
            args.host,
            args.port,
            loop,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
        )
        stack.callback(instance.server_close)
        if loop:
            loop.start()
        try:
            instance.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
