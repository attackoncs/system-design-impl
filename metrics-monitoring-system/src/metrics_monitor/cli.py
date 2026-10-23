import argparse
import json
from pathlib import Path
import threading
import time

from .alerts import Alerts, WebhookProvider
from .http import Exporter, server
from .pipeline import Collector, QueueAdapter, storage_loop
from .storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role", choices=["exporter", "collector", "api", "storage", "alerts", "archive"]
    )
    parser.add_argument("--root", default=".runtime/monitor")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9820)
    parser.add_argument("--instance", default="local")
    parser.add_argument("--targets", help="reloadable JSON target list")
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.interval >= 0.2:
        parser.error("interval must be >=0.2 seconds")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    if args.role in ("api", "storage", "alerts", "archive"):
        store = Store(
            root / "timeseries.sqlite",
            max_series=config.get("max_series", 10000),
            per_metric=config.get("per_metric", 1000),
        )
        alerts = Alerts(store)
    if args.role == "archive":
        if args.start is None or args.end is None or not args.output:
            parser.error("archive requires --start, --end and --output")
        print(json.dumps(store.archive(args.output, args.start, args.end)))
        return
    if args.role == "storage":
        try:
            storage_loop(
                store,
                QueueAdapter(
                    config["queue_url"],
                    config["queue_token"],
                    config.get("topic", "metrics"),
                ),
                stop,
            )
        except KeyboardInterrupt:
            stop.set()
        return
    if args.role == "alerts":
        provider = WebhookProvider(config.get("destinations", {}))
        try:
            while not stop.is_set():
                alerts.evaluate()
                for _ in range(100):
                    if not alerts.notify_once(provider):
                        break
                stop.wait(args.interval)
        except KeyboardInterrupt:
            stop.set()
        return
    threads = []
    if args.role == "exporter":
        httpd = server(args.host, args.port, config, exporter=Exporter(args.instance))
    elif args.role == "api":
        httpd = server(args.host, args.port, config, store=store, alerts=alerts)
    else:
        collector = Collector(
            root / "collector.sqlite",
            max_bytes=config.get("spool_bytes", 16 * 1024 * 1024),
        )
        queue = QueueAdapter(
            config["queue_url"], config["queue_token"], config.get("topic", "metrics")
        )

        def dispatch():
            while not stop.is_set():
                worked = collector.flush_once(queue)
                if not worked:
                    stop.wait(0.2)

        def discover():
            while not stop.is_set():
                try:
                    targets = (
                        json.loads(Path(args.targets).read_text(encoding="utf-8"))
                        if args.targets
                        else []
                    )
                    if not isinstance(targets, list):
                        raise ValueError("target list required")
                    for target in targets:
                        collector.scrape(target)
                except Exception as exc:
                    with collector.transaction(True) as db:
                        db.execute(
                            "INSERT INTO scrapes VALUES('_discovery',?,?) ON CONFLICT(target) DO UPDATE SET updated=excluded.updated,error=excluded.error",
                            (time.time(), type(exc).__name__),
                        )
                stop.wait(args.interval)

        threads = [
            threading.Thread(target=dispatch, daemon=True),
            threading.Thread(target=discover, daemon=True),
        ]
        for thread in threads:
            thread.start()
        httpd = server(args.host, args.port, config, collector=collector)
    print(
        f"{args.role} listening on http://{args.host}:{httpd.server_port}", flush=True
    )
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        for thread in threads:
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
