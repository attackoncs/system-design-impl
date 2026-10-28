import ipaddress
from .transport import endpoint
import argparse
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .common import Error, integer, request
from .contracts import fields, settings as validate_settings, MAX_TIMESTAMP
from .transport import loads, secret
from contextlib import ExitStack
import signal
import time
import logging
from .http import server
from .market import Market
from .storage import Engine, Sequencer


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
            "gateway",
            "engines",
            "market",
            "admin_token",
            "internal_token",
            "owners",
            "feed_retention",
            "simulation_timestamp",
            "settings",
        },
    )
    fields(config["engines"], {"primary", "backup"})
    for key in ("gateway", "market"):
        config[key] = base_url(config[key])
    config["engines"] = {
        key: base_url(value) for key, value in config["engines"].items()
    }
    if len({config["gateway"], config["market"], *config["engines"].values()}) != 4:
        raise ValueError("four distinct canonical role endpoints required")
    config["settings"] = validate_settings(config["settings"])
    if not isinstance(config["owners"], dict) or set(config["owners"]) != set(
        config["settings"]["accounts"]
    ):
        raise ValueError("account credentials must match settings owners")
    tokens = [
        config["admin_token"],
        config["internal_token"],
        *config["owners"].values(),
    ]
    if any(not secret(value) or not 24 <= len(value) <= 256 for value in tokens) or len(
        set(tokens)
    ) != len(tokens):
        raise ValueError("distinct ASCII credentials of 24..256 characters required")
    integer(config["simulation_timestamp"], 1, MAX_TIMESTAMP)
    integer(config["feed_retention"], 2, 1000)
    return config


def poll_once(role, store, config, stop):
    deadline = time.monotonic() + 5
    if role == "market":
        store.poll(config, deadline=deadline, cancel=stop)
        return
    after = store.cursor()["seq"]
    response = request(
        config["gateway"] + "/internal/events?after=" + str(after),
        config["internal_token"],
        timeout=2,
        deadline=deadline,
        cancel=stop,
    )
    fields(response, {"events"})
    if not isinstance(response["events"], list) or len(response["events"]) > 100:
        raise ValueError("bounded event list required")
    for row in response["events"]:
        if stop.is_set() or time.monotonic() >= deadline:
            return
        store.apply(row)


def serve_role(args, config):
    root, stop = Path(args.root), threading.Event()
    if args.role == "gateway":
        store = Sequencer(
            root / "sequencer.sqlite",
            config["settings"],
            config["simulation_timestamp"],
        )
        url = config["gateway"]
    elif args.role in ("primary", "backup"):
        store = Engine(root / (args.role + ".sqlite"), config["feed_retention"])
        url = config["engines"][args.role]
    else:
        store = Market(root / "market.sqlite")
        url = config["market"]

    def poll():
        while not stop.is_set():
            try:
                poll_once(args.role, store, config, stop)
            except (Error, OSError, ValueError, TypeError):
                stop.wait(0.1)
            except Exception:
                logging.getLogger(__name__).warning("exchange poll cycle failed")
                stop.wait(0.1)
            stop.wait(0.02)

    thread = None
    parsed = urlsplit(url)
    with ExitStack() as cleanup:
        instance = server(args.role, store, config, None, parsed.hostname, parsed.port)

        def join():
            if thread is not None and thread.ident is not None:
                thread.join()

        cleanup.callback(join)
        cleanup.callback(instance.server_close)
        cleanup.callback(stop.set)
        if args.role != "gateway":
            thread = threading.Thread(
                target=poll, name="exchange-" + args.role + "-poll"
            )
            thread.start()
        instance.serve_forever(poll_interval=0.05)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["gateway", "primary", "backup", "market"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    args = parser.parse_args(argv)
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
