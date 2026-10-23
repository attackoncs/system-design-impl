import argparse
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from .app import App
from .cache import Cache
from .common import identifier
from .http import server
from .store import Consumer, Store
from .workers import (
    PeriodicWorker,
    replicate_once,
    consume_local_once,
    consume_remote_once,
)


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config["stage"] not in ("single", "balanced", "sharded"):
        raise ValueError("unknown stage")
    urls = list(config["services"].values())
    if not urls or len(urls) != len(set(urls)):
        raise ValueError("distinct service endpoints required")
    for value in urls:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or not ipaddress.ip_address(url.hostname).is_loopback
            or not url.port
            or url.username
            or url.path not in ("", "/")
        ):
            raise ValueError("only loopback HTTP experiment endpoints supported")
    tokens = [
        config["admin_token"],
        config["internal_token"],
        *config["users"].values(),
    ]
    if (
        not config["users"]
        or any(not isinstance(token, str) or len(token) < 24 for token in tokens)
        or len(tokens) != len(set(tokens))
    ):
        raise ValueError("distinct lab credentials required")
    for user in config["users"]:
        identifier(user)
    expected = {"single": 0, "balanced": 1, "sharded": 2}[config["stage"]]
    if len(config["primaries"]) != expected or len(config["replicas"]) != expected:
        raise ValueError("stage/shard count mismatch")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "node", help="single, lb, web0/1, primary0/1, replica0/1, cache, worker or edge"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.node not in config["services"]:
        parser.error("node not part of this topology")
    node = args.node
    role = (
        "primary"
        if node.startswith("primary")
        else "replica"
        if node.startswith("replica")
        else "web"
        if node.startswith("web")
        else node
    )
    index = int(node[-1]) if role in ("primary", "replica", "web") else 0
    root = Path(args.root)
    worker = None
    if role in ("primary", "replica"):
        store = Store(root / (node + ".sqlite"))
        store.bind_partition(len(config["primaries"]), index)
    elif role == "cache":
        store = Cache(capacity=32, ttl=2)
    elif role == "worker":
        store = Consumer(root / "worker.sqlite")
    elif role == "single":
        store = App(config, node, Store(root / "single.sqlite"))
    elif role == "web":
        store = App(config, node)
    else:
        store = None
    if role == "replica":
        worker = PeriodicWorker(
            lambda: replicate_once(
                store, config["primaries"][index], config["internal_token"]
            ),
            "replication",
        )
    elif role in ("worker", "single"):
        consumer = (
            store if role == "worker" else Consumer(root / "single-consumer.sqlite")
        )
        if role == "single":
            step = lambda: consume_local_once(store.store, consumer)
        else:
            step = lambda: consume_remote_once(
                config["primaries"], config["internal_token"], consumer
            )
        worker = PeriodicWorker(step, "outbox-consumer")
    parsed = urlsplit(config["services"][node])
    instance = server(role, store, config, index, parsed.hostname, parsed.port)
    try:
        if worker is not None:
            worker.start()
        instance.serve_forever(poll_interval=0.05)
    finally:
        if worker is not None:
            worker.close()
        instance.server_close()


if __name__ == "__main__":
    main()
