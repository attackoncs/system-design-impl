import argparse
import json
from pathlib import Path

from .common import identifier, integer
from .http import server
from .payment import SimulatedLedger
from .store import Store
from .worker import run


def load_config(path):
    config = json.loads(Path(path).read_text())
    tokens = []
    for role in ("admin", "internal", "payment"):
        tokens.append(config[role + "_token"])
    if not isinstance(config["users"], dict) or not config["users"]:
        raise ValueError("configured guest identities required")
    for user, token in config["users"].items():
        identifier(user)
        tokens.append(token)
    if any(not isinstance(t, str) or len(t)<24 for t in tokens) or len(tokens)!=len(set(tokens)):
        raise ValueError("distinct credentials of at least 24 characters required")
    urls = config["shard_urls"]
    if not isinstance(urls, list) or not 1<=len(urls)<=32 or len(set(urls))!=len(urls):
        raise ValueError("1..32 distinct shard URLs required")
    integer(config.get("lease_seconds", 2), "lease_seconds", 1, 60)
    integer(config.get("hold_seconds", 120), "hold_seconds", 1, 900)
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["gateway", "shard", "worker", "payment"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9830)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args()
    config = load_config(args.config)
    integer(args.shard, "shard", 0, len(config["shard_urls"])-1)
    root, store = Path(args.root), None
    if args.role == "worker":
        run(config, args.shard)
        return
    if args.role == "shard":
        store = Store(root/f"shard-{args.shard}.sqlite", args.shard, len(config["shard_urls"]), config.get("hold_seconds", 120))
    if args.role == "payment":
        store = SimulatedLedger(root/"simulated-payments.sqlite")
    instance = server(args.role, config, store, args.host, args.port)
    try:
        instance.serve_forever(poll_interval=0.1)
    finally:
        instance.server_close()


if __name__ == "__main__":
    main()
