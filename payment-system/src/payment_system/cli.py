import argparse
import ipaddress
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .common import identifier
from .http import server
from .provider import MockPSP
from .receiver import Receiver
from .store import Store
from .worker import run


def load_config(path):
    config = json.loads(Path(path).read_text())
    tokens = [
        config[r + "_token"] for r in ("admin", "internal", "provider", "event")
    ] + [config["webhook_secret"]]
    if not isinstance(config["merchants"], dict) or not config["merchants"]:
        raise ValueError("merchant identities required")
    for merchant, token in config["merchants"].items():
        identifier(merchant)
        tokens.append(token)
    if any(not isinstance(t, str) or len(t) < 24 for t in tokens) or len(tokens) != len(
        set(tokens)
    ):
        raise ValueError("distinct credentials of at least 24 characters required")
    for key in ("api_url", "provider_url", "receiver_url"):
        url = urlsplit(config[key])
        if url.scheme != "http" or not ipaddress.ip_address(url.hostname).is_loopback:
            raise ValueError(
                "only loopback mock endpoints supported; no real PSP adapter enabled"
            )
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "provider", "worker", "receiver"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9870)
    args = parser.parse_args()
    config = load_config(args.config)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("simulated reference binds loopback only")
    root = Path(args.root)
    if args.role == "worker":
        run(config)
        return
    stop = threading.Event()
    if args.role == "api":
        store = Store(root / "payments.sqlite", tuple(config["merchants"]))
    elif args.role == "provider":
        store = MockPSP(root / "mock-psp.sqlite", config)

        def dispatch():
            while not stop.is_set():
                try:
                    busy = store.dispatch_once()
                except Exception:
                    busy = False
                stop.wait(0.02 if busy else 0.1)

        threading.Thread(target=dispatch, daemon=True).start()
    else:
        store = Receiver(root / "accounting-consumer.sqlite")
    instance = server(args.role, config, store, args.host, args.port)
    try:
        instance.serve_forever(poll_interval=0.1)
    finally:
        stop.set()
        instance.server_close()


if __name__ == "__main__":
    main()
