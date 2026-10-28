"""Local configuration and deterministic ownership of role resources."""

import argparse
from contextlib import ExitStack
import ipaddress
from pathlib import Path
import signal
import threading

from .common import identifier, integer
from .http import server
from .journal import Journal
from .projection import Projection
from .service import Service
from .transport import endpoint, loads, secret


def local_host(value):
    if value == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("loopback address required") from None
    if not address.is_loopback:
        raise ValueError("loopback address required")
    return str(address)


def local_url(value):
    parsed = endpoint(value)
    if parsed.scheme != "http" or parsed.path not in ("", "/") or parsed.query:
        raise ValueError("plain loopback HTTP base URL required")
    host = local_host(parsed.hostname)
    host = "[" + host + "]" if ":" in host else host
    return f"http://{host}:{parsed.port or 80}"


def load_config(path):
    config = loads(Path(path).read_bytes())
    if not isinstance(config, dict):
        raise ValueError("configuration object required")
    try:
        tokens = [
            config[k + "_token"] for k in ("admin", "read", "game", "internal")
        ] + [config["redis_password"]]
        if any(not secret(token) or len(token) < 24 for token in tokens) or len(
            set(tokens)
        ) != len(tokens):
            raise ValueError(
                "distinct private ASCII credentials of at least 24 characters required"
            )
        identifier(config["namespace"])
        urls = config["projection_urls"]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 16:
            raise ValueError("1..16 projection shards required")
        config["authority_url"] = local_url(config["authority_url"])
        config["projection_urls"] = [local_url(url) for url in urls]
        urls = [config["authority_url"], *config["projection_urls"]]
        if len(set(urls)) != len(urls):
            raise ValueError("distinct authority and projection endpoints required")
        pairs = config["redis_nodes"]
        if not isinstance(pairs, list) or len(pairs) != len(config["projection_urls"]):
            raise ValueError("consistent projection and Redis topology required")
        endpoints = []
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("one primary and replica endpoint required per shard")
            for node in pair:
                if not isinstance(node, dict) or set(node) != {"host", "port"}:
                    raise ValueError("Redis node requires host and port")
                node["host"] = local_host(node["host"])
                integer(node["port"], "Redis port", 1024, 65535)
                endpoints.append((node["host"], node["port"]))
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("Redis endpoints must be distinct")
        http_endpoints = {(endpoint(url).hostname, endpoint(url).port) for url in urls}
        if http_endpoints.intersection(endpoints):
            raise ValueError("HTTP and Redis endpoints must be distinct")
        integer(config.get("late_grace", 172800), "late_grace", 0, 31 * 86400)
    except KeyError:
        raise ValueError("required configuration field missing") from None
    return config


def serve_role(args, config):
    """Bind before starting the owned projection thread and drain every exit."""
    stop, instance, store, threads = threading.Event(), None, None, []
    try:
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
        instance = server(args.role, config, store, args.host, args.port)
        if args.role == "projection":
            thread = threading.Thread(
                target=store.run, args=(stop,), name="leaderboard-projection"
            )
            thread.start()
            threads.append(thread)
        instance.serve_forever(poll_interval=0.1)
    finally:
        with ExitStack() as cleanup:
            close = getattr(store, "close", None)
            if close is not None:
                cleanup.callback(close)
            for thread in threads:
                cleanup.callback(thread.join)
            if instance is not None:
                cleanup.callback(instance.server_close)
            cleanup.callback(stop.set)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["authority", "projection"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9860)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    args.host = local_host(args.host)
    integer(args.port, "HTTP port", 1, 65535)
    previous = {}

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
    try:
        serve_role(args, config)
    except KeyboardInterrupt:
        pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
