"""Loopback-only simulation roles with owned background worker lifetimes."""

import argparse
from contextlib import ExitStack
import ipaddress
from pathlib import Path
import signal
import threading
import time
from urllib.parse import urlsplit
from .common import identifier, integer
from .transport import loads, secret
from .http import server
from .provider import MockPSP
from .receiver import Receiver
from .store import Store
from .worker import run


def local_host(host):
    host = "127.0.0.1" if host == "localhost" else host
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("simulated reference binds loopback only")
    return str(ipaddress.ip_address(host))


def load_config(path):
    config = loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("merchants"), dict)
        or not config["merchants"]
    ):
        raise ValueError("merchant identities required")
    tokens = [
        config.get(role + "_token")
        for role in ("admin", "internal", "provider", "event")
    ] + [config.get("webhook_secret")]
    for merchant, token in config["merchants"].items():
        identifier(merchant)
        tokens.append(token)
    if any(not secret(value) or not 24 <= len(value) <= 256 for value in tokens) or len(
        set(tokens)
    ) != len(tokens):
        raise ValueError(
            "distinct private ASCII credentials of 24..256 characters required"
        )
    urls = []
    for key in ("api_url", "provider_url", "receiver_url"):
        target = urlsplit(config[key])
        if (
            target.scheme != "http"
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
            or target.path not in ("", "/")
        ):
            raise ValueError("local simulated base URLs required")
        host = local_host(target.hostname)
        port = target.port or 80
        integer(port, "endpoint port", 1, 65535)
        config[key] = (
            "http://" + ("[" + host + "]" if ":" in host else host) + ":" + str(port)
        )
        urls.append(config[key])
    if len(set(urls)) != len(urls):
        raise ValueError("distinct simulation role endpoints required")
    integer(config.get("worker_concurrency", 4), "worker concurrency", 1, 16)
    return config


def serve_role(args, config):
    stop = threading.Event()
    if args.role == "worker":
        try:
            run(config, stop=stop, concurrency=config.get("worker_concurrency", 4))
        finally:
            stop.set()
        return
    root = Path(args.root)
    store = (
        Store(root / "payments.sqlite", tuple(config["merchants"]))
        if args.role == "api"
        else MockPSP(root / "mock-psp.sqlite", config)
        if args.role == "provider"
        else Receiver(root / "accounting-consumer.sqlite")
    )
    instance, threads = None, []
    try:
        instance = server(args.role, config, store, args.host, args.port)
        if args.role == "provider":

            def dispatch():
                while not stop.is_set():
                    try:
                        busy = store.dispatch_once(
                            deadline=time.monotonic() + 5, cancel=stop
                        )
                    except Exception:
                        busy = False
                    stop.wait(0.02 if busy else 0.1)

            thread = threading.Thread(target=dispatch, name="simulated-psp-callbacks")
            thread.start()
            threads.append(thread)
        instance.serve_forever(poll_interval=0.1)
    finally:
        with ExitStack() as cleanup:
            for thread in threads:
                cleanup.callback(thread.join)
            if instance is not None:
                cleanup.callback(instance.server_close)
            cleanup.callback(stop.set)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "provider", "worker", "receiver"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9870)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    args.host = local_host(args.host)
    integer(args.port, "HTTP port", 1, 65535)
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
