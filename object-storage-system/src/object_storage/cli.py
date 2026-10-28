"""Local role configuration and deterministic resource ownership."""

import argparse
import ipaddress
from pathlib import Path
import signal

from .common import identifier, integer
from .http import server
from .metadata import Metadata
from .node import DataNode
from .service import Catalog, Service
from .transport import endpoint, loads, secret


def local_url(value):
    parsed = endpoint(value)
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        local = parsed.hostname == "localhost"
    if (
        not local
        or parsed.scheme != "http"
        or parsed.path not in ("", "/")
        or parsed.query
    ):
        raise ValueError("plain loopback HTTP node base URL required")
    host = (
        "127.0.0.1"
        if parsed.hostname == "localhost"
        else str(ipaddress.ip_address(parsed.hostname))
    )
    host = "[" + host + "]" if ":" in host else host
    return f"http://{host}:{parsed.port or 80}"


def load_config(path):
    config = loads(Path(path).read_bytes())
    if not isinstance(config, dict):
        raise ValueError("configuration object required")
    users = config.get("users")
    if not isinstance(users, dict) or not 1 <= len(users) <= 1000:
        raise ValueError("1..1000 users required")
    tokens = [
        config[k] for k in ("admin_token", "internal_token", "continuation_secret")
    ]
    for user, token in users.items():
        identifier(user)
        tokens.append(token)
    if any(not secret(token) or len(token) < 24 for token in tokens) or len(
        tokens
    ) != len(set(tokens)):
        raise ValueError(
            "distinct private ASCII credentials of at least 24 characters required"
        )
    all_urls = []
    for name, low, high in (("data_urls", 3, 3), ("metadata_urls", 1, 32)):
        values = config[name]
        if not isinstance(values, list) or not low <= len(values) <= high:
            raise ValueError("invalid " + name)
        config[name] = [local_url(value) for value in values]
        all_urls.extend(config[name])
    if len(set(all_urls)) != len(all_urls):
        raise ValueError("distinct data and metadata endpoints required")
    integer(config.get("write_quorum", 2), "write_quorum", 2, 3)
    return config


def serve_role(args, config):
    root = Path(args.root)
    if args.role == "api":
        store = Service(config, Catalog(root / "catalog.sqlite"))
    elif args.role == "metadata":
        integer(args.index, "metadata index", 0, len(config["metadata_urls"]) - 1)
        store = Metadata(
            root / f"metadata-{args.index}.sqlite",
            args.index,
            len(config["metadata_urls"]),
        )
    else:
        integer(args.index, "data index", 0, 2)
        store = DataNode(root / f"data-{args.index}")
    instance = None
    try:
        instance = server(args.role, config, store, args.host, args.port)
        instance.serve_forever(poll_interval=0.1)
    finally:
        try:
            if instance is not None:
                instance.server_close()
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "metadata", "data"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9850)
    parser.add_argument("--index", type=int, default=0)
    args = parser.parse_args()
    config = load_config(args.config)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("reference lab binds loopback only")
    integer(args.port, "port", 1, 65535)
    previous = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, interrupted)
        serve_role(args, config)
    except KeyboardInterrupt:
        pass
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
