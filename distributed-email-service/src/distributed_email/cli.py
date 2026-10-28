import argparse
from contextlib import ExitStack
import ipaddress
from pathlib import Path
import signal
import threading

from .common import identifier, integer
from .http import server
from .objects import ObjectStore
from .smtp import Spool, smtp_server
from .store import MailStore
from .worker import replicate, run_sender
from .transport import endpoint, loads, secret


def load_config(path):
    config = loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("configuration must be an object")
    tokens = [config[r + "_token"] for r in ("admin", "internal", "smtp")]
    if not isinstance(config["users"], dict) or not config["users"]:
        raise ValueError("guest mailbox identities required")
    for user, token in config["users"].items():
        identifier(user)
        tokens.append(token)
    if any(not secret(t) or len(t) < 24 for t in tokens) or len(tokens) != len(
        set(tokens)
    ):
        raise ValueError("distinct credentials of at least 24 characters required")
    if not isinstance(config["primary_urls"], list) or not isinstance(
        config["replica_urls"], list
    ):
        raise ValueError("mailbox URLs must be arrays")
    if not 1 <= len(config["primary_urls"]) <= 32 or len(config["primary_urls"]) != len(
        config["replica_urls"]
    ):
        raise ValueError("one read replica URL required per primary")
    urls = config["primary_urls"] + config["replica_urls"] + [config["object_url"]]
    for url in urls:
        parsed = endpoint(url)
        if parsed.scheme != "http" or parsed.path not in ("", "/") or parsed.query:
            raise ValueError("plain HTTP node base URL required")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if not loopback:
            raise ValueError("all node endpoints must be loopback")
    if len(set(url.rstrip("/") for url in urls)) != len(urls):
        raise ValueError("object and mailbox node URLs must be distinct")
    config["primary_urls"] = [url.rstrip("/") for url in config["primary_urls"]]
    config["replica_urls"] = [url.rstrip("/") for url in config["replica_urls"]]
    config["object_url"] = config["object_url"].rstrip("/")
    if len(set(config["primary_urls"] + config["replica_urls"])) != 2 * len(
        config["primary_urls"]
    ):
        raise ValueError("node URLs must be distinct")
    integer(config["smtp_port"], "smtp_port", 1024, 65535)
    if config.get("scanner", "quarantine-all") not in (
        "test-markers",
        "quarantine-all",
    ):
        raise ValueError("unsupported scanner; no real scanner adapter configured")
    return config


def serve_role(args, config):
    """Bind all listeners before starting owned threads; drain every exit path."""
    stop, state, threads = threading.Event(), {}, []
    instance, smtp = None, None
    try:
        if args.role == "worker":
            run_sender(config, args.shard, stop=stop)
            return
        root, store = Path(args.root), None
        if args.role in ("primary", "replica"):
            store = MailStore(
                root / f"{args.role}-{args.shard}.sqlite",
                args.shard,
                len(config["primary_urls"]),
                args.role == "replica",
            )
        if args.role == "objects":
            store = ObjectStore(root / "objects")
        if args.role == "smtp":
            store = Spool(root / "smtp-spool.sqlite")
            smtp = smtp_server(config, store, args.host, config["smtp_port"])
        instance = server(args.role, config, store, args.host, args.port, state)

        def start(target, name, arguments=()):
            thread = threading.Thread(target=target, args=arguments, name=name)
            thread.start()
            threads.append(thread)

        if args.role == "replica":
            start(replicate, "email-replication", (config, store, stop, state))
        if smtp:
            start(smtp.serve_forever, "email-smtp")

            def receive_loop():
                while not stop.is_set():
                    try:
                        busy = store.process_once(config, stop=stop)
                        state["receive_error"] = None
                    except Exception:
                        state["receive_error"] = "receive operation failed; will retry"
                        busy = False
                    stop.wait(0.02 if busy else 0.1)

            start(receive_loop, "email-receive")
        instance.serve_forever(poll_interval=0.1)
    finally:
        with ExitStack() as cleanup:
            for thread in threads:
                cleanup.callback(thread.join)
            if smtp is not None:
                cleanup.callback(smtp.server_close)
                if any(thread.name == "email-smtp" for thread in threads):
                    cleanup.callback(smtp.shutdown)
            if instance is not None:
                cleanup.callback(instance.server_close)
            cleanup.callback(stop.set)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role", choices=["api", "primary", "replica", "objects", "smtp", "worker"]
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9840)
    parser.add_argument("--shard", type=int, default=0)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    integer(args.shard, "shard", 0, len(config["primary_urls"]) - 1)
    integer(args.port, "HTTP port", 1, 65535)
    if not ipaddress.ip_address(args.host).is_loopback:
        parser.error("this local SMTP reference only binds loopback addresses")
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
