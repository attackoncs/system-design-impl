"""Validated loopback configuration and owned process lifetimes."""

import argparse
from contextlib import ExitStack
import ipaddress
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit

from .common import Error, identifier, integer, request
from .contracts import fields
from .coordinator import Coordinator
from .http import server
from .journal import Replica
from .shard import Shard
from .transport import endpoint, loads, secret


def local_host(value):
    host = "127.0.0.1" if value == "localhost" else value
    address = ipaddress.ip_address(host)
    if not address.is_loopback:
        raise ValueError("loopback reference endpoints required")
    return str(address)


def base_url(value):
    parsed = endpoint(value)
    if (
        parsed.scheme != "http"
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        raise ValueError("explicit loopback HTTP base URL required")
    host = local_host(parsed.hostname)
    integer(parsed.port, 1, 65535)
    return (
        "http://" + ("[" + host + "]" if ":" in host else host) + ":" + str(parsed.port)
    )


def load_config(path):
    config = loads(Path(path).read_text(encoding="utf-8"))
    fields(
        config,
        {
            "coordinator",
            "shards",
            "replicas",
            "epoch",
            "leader",
            "admin_token",
            "internal_token",
            "owners",
            "accounts",
        },
    )
    if (
        not isinstance(config["shards"], list)
        or len(config["shards"]) != 2
        or not isinstance(config["replicas"], list)
        or len(config["replicas"]) != 3
    ):
        raise ValueError("two shards and three replicas required")
    config["coordinator"] = base_url(config["coordinator"])
    for key in ("shards", "replicas"):
        config[key] = [base_url(value) for value in config[key]]
    urls = [config["coordinator"], *config["shards"], *config["replicas"]]
    if len(set(urls)) != len(urls):
        raise ValueError("distinct canonical role endpoints required")
    if not isinstance(config["owners"], dict) or not 1 <= len(config["owners"]) <= 100:
        raise ValueError("1..100 owner identities required")
    for owner in config["owners"]:
        identifier(owner)
    tokens = [
        config["admin_token"],
        config["internal_token"],
        *config["owners"].values(),
    ]
    if any(not secret(value) or not 24 <= len(value) <= 256 for value in tokens) or len(
        set(tokens)
    ) != len(tokens):
        raise ValueError(
            "distinct private ASCII credentials of 24..256 characters required"
        )
    integer(config["epoch"], 1)
    identifier(config["leader"])
    if (
        not isinstance(config["accounts"], dict)
        or not 1 <= len(config["accounts"]) <= 100
    ):
        raise ValueError("reference supports 1..100 preprovisioned accounts")
    for account, value in config["accounts"].items():
        identifier(account)
        fields(value, {"owner", "opening"})
        integer(value["opening"], 0, 100_000_000)
        if value["owner"] not in config["owners"]:
            raise ValueError("unknown account owner")
    return config


def run_worker(config, stop):
    while not stop.is_set():
        try:
            value = request(
                config["coordinator"] + "/internal/step",
                config["internal_token"],
                {},
                timeout=12,
                cancel=stop,
            )
            delay = 0.1 if value.get("idle") else 0.02
        except (Error, OSError, ValueError):
            # Explicitly retain the same durable commands after an ambiguous RPC.
            # KeyboardInterrupt/SystemExit remain outside this recovery path.
            delay = 0.15
        stop.wait(delay)


def serve_role(args, config):
    stop = threading.Event()
    if args.role == "worker":
        try:
            run_worker(config, stop)
        finally:
            stop.set()
        return
    root = Path(args.root)
    if args.role == "coordinator":
        store = Coordinator(root / "coordinator.sqlite", config)
        url = config["coordinator"]
    elif args.role == "shard":
        store = Shard(
            root / ("shard-" + str(args.index) + ".sqlite"),
            args.index,
            2,
            {key: value["opening"] for key, value in config["accounts"].items()},
            config["epoch"],
            config["leader"],
        )
        url = config["shards"][args.index]
    else:
        store = Replica(
            root / ("replica-" + str(args.index) + ".sqlite"),
            config["epoch"],
            config["leader"],
        )
        url = config["replicas"][args.index]
    with ExitStack() as cleanup:
        if args.role == "coordinator":
            cleanup.callback(store.close)
        parsed = urlsplit(url)
        instance = server(args.role, store, config, parsed.hostname, parsed.port)
        cleanup.callback(instance.server_close)
        instance.serve_forever(poll_interval=0.05)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["coordinator", "worker", "shard", "replica"])
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    args = parser.parse_args(argv)
    if args.role in {"shard", "replica"} and args.index not in range(
        2 if args.role == "shard" else 3
    ):
        parser.error("invalid participant index")
    config = load_config(args.config)
    previous = {}

    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for value in (signal.SIGINT, signal.SIGTERM):
            previous[value] = signal.signal(value, interrupt)
    try:
        serve_role(args, config)
    except KeyboardInterrupt:
        pass
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)


if __name__ == "__main__":
    main()
