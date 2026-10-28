"""Explicit application composition with owned maintenance and bind-first startup."""

import argparse
from contextlib import ExitStack
from pathlib import Path

from .common import identifier, integer
from .http import server
from .lifecycle import Worker
from .payment import SimulatedLedger
from .store import Store
from .transport import endpoint, loads, seconds, secret
from .worker import run


def load_config(path):
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("configuration exceeds 1 MiB")
    config = loads(raw)
    if not isinstance(config, dict):
        raise ValueError("configuration object required")
    tokens = [config[role + "_token"] for role in ("admin", "internal", "payment")]
    if (
        not isinstance(config.get("users"), dict)
        or not 1 <= len(config["users"]) <= 10000
    ):
        raise ValueError("1..10000 guest identities required")
    for user, token in config["users"].items():
        identifier(user)
        tokens.append(token)
    if any(not secret(token) or len(token) < 24 for token in tokens) or len(
        tokens
    ) != len(set(tokens)):
        raise ValueError(
            "distinct private credentials of at least 24 characters required"
        )
    urls = config["shard_urls"]
    if (
        not isinstance(urls, list)
        or not 1 <= len(urls) <= 32
        or any(not isinstance(url, str) for url in urls)
        or len(set(urls)) != len(urls)
    ):
        raise ValueError("1..32 distinct shard URLs required")
    for url in [*urls, config["payment_url"]]:
        parsed = endpoint(url)
        if parsed.query or parsed.path not in ("", "/"):
            raise ValueError("service URLs must be origins")
    integer(config.get("lease_seconds", 2), "lease_seconds", 1, 60)
    integer(config.get("hold_seconds", 120), "hold_seconds", 1, 900)
    seconds(config.get("calendar_interval", 5))
    return config


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["gateway", "shard", "worker", "payment"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9830)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        integer(args.port, "port", 0, 65535)
        integer(args.concurrency, "HTTP capacity", 1, 256)
        seconds(args.request_timeout)
    except ValueError as exc:
        parser.error(str(exc))
    config = load_config(args.config)
    integer(args.shard, "shard", 0, len(config["shard_urls"]) - 1)
    if args.role == "worker":
        run(config, args.shard)
        return
    root, store, calendar = Path(args.root), None, None
    workers = {}
    with ExitStack() as stack:
        if args.role == "shard":
            from .calendar_policy import CalendarPolicies

            store = Store(
                root / f"shard-{args.shard}.sqlite",
                args.shard,
                len(config["shard_urls"]),
                config.get("hold_seconds", 120),
            )
            calendar = CalendarPolicies(store)
            worker = Worker(calendar.roll_once, config.get("calendar_interval", 5))
            workers["calendar"] = worker
            stack.callback(worker.close)
        elif args.role == "payment":
            store = SimulatedLedger(root / "simulated-payments.sqlite")
        instance = server(
            args.role,
            config,
            store,
            args.host,
            args.port,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
            calendar=calendar,
            workers=workers,
        )
        stack.callback(instance.server_close)
        for worker in workers.values():
            worker.start()
        try:
            instance.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
