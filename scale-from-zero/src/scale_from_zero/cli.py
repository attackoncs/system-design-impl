import argparse
import ipaddress
import re
import signal
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from .app import App
from .cache import Cache
from .common import identifier, integer
from .contracts import freeze, fields
from .transport import endpoint, loads, secret
from .http import server
from .store import Consumer, Store
from .workers import (
    PeriodicWorker,
    replicate_once,
    consume_local_once,
    consume_remote_once,
)


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
            "stage",
            "services",
            "entry",
            "webs",
            "primaries",
            "replicas",
            "cache",
            "edge",
            "admin_token",
            "internal_token",
            "users",
        },
    )
    stage = config["stage"]
    if stage not in {"single", "balanced", "sharded"} or not isinstance(
        config["services"], dict
    ):
        raise ValueError("valid stage and services required")
    services = config["services"] = {
        name: base_url(url) for name, url in config["services"].items()
    }
    if len(set(services.values())) != len(services):
        raise ValueError("distinct canonical role endpoints required")
    count = {"single": 0, "balanced": 1, "sharded": 2}[stage]
    web_nodes = [name for name in services if re.fullmatch(r"web[0-7]", name)]
    expected = (
        {"single"}
        if stage == "single"
        else {"primary" + str(i) for i in range(count)}
        | {"replica" + str(i) for i in range(count)}
        | {"cache", "lb", "worker", "edge"}
        | set(web_nodes)
    )
    if set(services) != expected or stage != "single" and not 1 <= len(web_nodes) <= 8:
        raise ValueError(
            "complete stage topology and 1..8 configured Web nodes required"
        )
    for key in ("primaries", "replicas", "webs"):
        if not isinstance(config[key], list):
            raise ValueError("endpoint list required")
        config[key] = [base_url(url) for url in config[key]]
    config["entry"] = base_url(config["entry"])
    for key in ("cache", "edge"):
        config[key] = base_url(config[key]) if config[key] is not None else None
    if config["primaries"] != [
        services["primary" + str(i)] for i in range(count)
    ] or config["replicas"] != [services["replica" + str(i)] for i in range(count)]:
        raise ValueError("ordered shard endpoints differ from services")
    if stage == "single":
        if (
            config["entry"] != services["single"]
            or config["webs"] != [services["single"]]
            or config["cache"] is not None
            or config["edge"] is not None
        ):
            raise ValueError("single-stage endpoints differ from services")
    elif (
        config["entry"] != services["lb"]
        or config["cache"] != services["cache"]
        or config["edge"] != services["edge"]
        or len(config["webs"]) != len(web_nodes)
        or set(config["webs"]) != {services[name] for name in web_nodes}
    ):
        raise ValueError("service references must match the configured Web pool")
    if not isinstance(config["users"], dict) or not 1 <= len(config["users"]) <= 100:
        raise ValueError("1..100 synthetic users required")
    for user in config["users"]:
        identifier(user)
    tokens = [
        config["admin_token"],
        config["internal_token"],
        *config["users"].values(),
    ]
    if any(not secret(value) or not 24 <= len(value) <= 256 for value in tokens) or len(
        set(tokens)
    ) != len(tokens):
        raise ValueError(
            "distinct ASCII lab credentials of 24..256 characters required"
        )
    return config


def serve_role(args, config):
    config = freeze(config)
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
                store,
                config["primaries"][index],
                config["internal_token"],
                deadline=time.monotonic() + 3,
                cancel=worker.cancel,
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
                config["primaries"],
                config["internal_token"],
                consumer,
                deadline=time.monotonic() + 5,
                cancel=worker.cancel,
            )
        worker = PeriodicWorker(step, "outbox-consumer")
    parsed = urlsplit(config["services"][node])
    instance = server(role, store, config, index, parsed.hostname, parsed.port)
    with ExitStack() as cleanup:
        if worker is not None:
            cleanup.callback(worker.close)
        cleanup.callback(instance.server_close)
        if worker is not None:
            cleanup.callback(worker.stop)
            worker.start()
        instance.serve_forever(poll_interval=0.05)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("node", help="a service name from the private topology")
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.node not in config["services"]:
        parser.error("node not part of this topology")
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
