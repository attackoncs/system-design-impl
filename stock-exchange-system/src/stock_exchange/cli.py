import argparse
import ipaddress
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .common import identifier, integer, request
from .http import server
from .market import Market
from .storage import Engine, Sequencer


def load_config(path):
    config = json.loads(Path(path).read_text())
    urls = [config["gateway"], config["market"], *config["engines"].values()]
    if set(config["engines"]) != {"primary", "backup"} or len(set(urls)) != 4:
        raise ValueError("four distinct loopback services required")
    for value in urls:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or not ipaddress.ip_address(url.hostname).is_loopback
            or url.username
            or not url.port
            or url.path not in ("", "/")
        ):
            raise ValueError("only loopback synthetic exchange endpoints supported")
    tokens = [
        config["admin_token"],
        config["internal_token"],
        *config["owners"].values(),
    ]
    if any(not isinstance(token, str) or len(token) < 24 for token in tokens) or len(
        tokens
    ) != len(set(tokens)):
        raise ValueError("distinct role credentials of at least 24 characters required")
    settings = config["settings"]
    if not 1 <= len(settings["symbols"]) <= 100 or len(set(settings["symbols"])) != len(
        settings["symbols"]
    ):
        raise ValueError("1..100 distinct simulated symbols required")
    if (
        set(settings["accounts"]) != set(config["owners"])
        or not 1 <= len(config["owners"]) <= 100
    ):
        raise ValueError("1..100 accounts with matching owner credentials required")
    for symbol in settings["symbols"]:
        identifier(symbol)
    for owner, value in settings["accounts"].items():
        identifier(owner)
        integer(value["cash"])
        if set(value["positions"]) - set(settings["symbols"]):
            raise ValueError("unknown opening position")
        for quantity in value["positions"].values():
            integer(quantity)
    session = settings["session"]
    integer(session["start_minute"], 0, 1439)
    integer(session["end_minute"], session["start_minute"] + 1, 1440)
    if not session["weekdays"] or any(
        type(day) is not int or not 0 <= day <= 6 for day in session["weekdays"]
    ):
        raise ValueError("UTC weekday schedule required")
    integer(settings["daily_limit"], 1, 1_000_000)
    integer(config["simulation_timestamp"], 1)
    integer(config["feed_retention"], 2, 1000)
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["gateway", "primary", "backup", "market"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--root", default=".runtime")
    args = parser.parse_args()
    config = load_config(args.config)
    root = Path(args.root)
    gate = threading.RLock()
    stop = threading.Event()
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

        def poll():
            while not stop.is_set():
                try:
                    with gate:
                        after = store.raw()["seq"]
                    rows = request(
                        config["gateway"] + "/internal/events?after=" + str(after),
                        config["internal_token"],
                        timeout=2,
                    )["events"]
                    for row in rows:
                        with gate:
                            store.apply(row)
                except Exception:
                    stop.wait(0.1)
                stop.wait(0.02)

        threading.Thread(target=poll, daemon=True).start()
    else:
        store = Market(root / "market.sqlite")
        url = config["market"]

        def poll():
            while not stop.is_set():
                try:
                    with gate:
                        store.poll(config)
                except Exception:
                    stop.wait(0.1)
                stop.wait(0.02)

        threading.Thread(target=poll, daemon=True).start()
    parsed = urlsplit(url)
    instance = server(args.role, store, config, gate, parsed.hostname, parsed.port)
    try:
        instance.serve_forever(poll_interval=0.05)
    finally:
        stop.set()
        instance.server_close()


if __name__ == "__main__":
    main()
