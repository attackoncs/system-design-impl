import argparse
import ipaddress
import json
from pathlib import Path
import threading

from .common import identifier, integer
from .http import server
from .journal import Journal
from .projection import Projection
from .service import Service


def load_config(path):
    config = json.loads(Path(path).read_text())
    tokens = [config[k + "_token"] for k in ("admin", "read", "game", "internal")] + [
        config["redis_password"]
    ]
    if any(not isinstance(t, str) or len(t) < 24 for t in tokens) or len(
        set(tokens)
    ) != len(tokens):
        raise ValueError("distinct credentials of at least 24 characters required")
    identifier(config["namespace"])
    count = len(config["projection_urls"])
    if (
        not 1 <= count <= 16
        or len(set(config["projection_urls"])) != count
        or len(config["redis_nodes"]) != count
    ):
        raise ValueError("consistent projection/Redis topology required")
    endpoints = []
    for pair in config["redis_nodes"]:
        if len(pair) != 2:
            raise ValueError("one primary and one replica endpoint required per shard")
        for node in pair:
            if not ipaddress.ip_address(node["host"]).is_loopback:
                raise ValueError("reference Redis endpoints must be loopback")
            integer(node["port"], "Redis port", 1024, 65535)
            endpoints.append((node["host"], node["port"]))
    if len(set(endpoints)) != len(endpoints):
        raise ValueError("Redis endpoints must be distinct")
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["authority", "projection"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9860)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args()
    config = load_config(args.config)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("reference servers bind loopback only")
    stop = threading.Event()
    if args.role == "authority":
        store = Service(
            config,
            Journal(
                Path(args.root) / "journal.sqlite",
                len(config["projection_urls"]),
                config.get("late_grace", 172800),
            ),
        )
    else:
        integer(args.shard, "shard", 0, len(config["projection_urls"]) - 1)
        store = Projection(config, args.shard)
        threading.Thread(target=store.run, args=(stop,), daemon=True).start()
    instance = server(args.role, config, store, args.host, args.port)
    try:
        instance.serve_forever(poll_interval=0.1)
    finally:
        stop.set()
        instance.server_close()


if __name__ == "__main__":
    main()
