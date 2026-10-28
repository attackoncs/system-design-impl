"""CLI composition root: bind first, then start owned workers, drain on exit."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import threading

from .alerts import Alerts, WebhookProvider
from .http import Exporter, server
from .lifecycle import Worker
from .pipeline import Collector, QueueAdapter, storage_loop
from .storage import Store
from .transport import loads, seconds


def _parser():
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
    parser.add_argument(
        "--rules-file",
        "--rules",
        dest="rules_file",
        help="bounded JSON rule file loaded atomically before serving",
    )
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=10)
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--output")
    return parser


def _queue(config):
    return QueueAdapter(
        config["queue_url"],
        config["queue_token"],
        config.get("topic", "metrics"),
        partitions=config.get("queue_partitions", 1),
    )


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if seconds(args.interval) < 0.2:
            raise ValueError("interval must be >=0.2 seconds")
        seconds(args.request_timeout)
        if not 1 <= args.concurrency <= 256 or not 0 <= args.port <= 65535:
            raise ValueError("invalid HTTP capacity or port")
    except ValueError as exc:
        parser.error(str(exc))
    config = loads(Path(args.config).read_bytes())
    if not isinstance(config, dict):
        parser.error("configuration object required")
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    with ExitStack() as stack:
        if args.role in ("api", "storage", "alerts", "archive"):
            store = Store(
                root / "timeseries.sqlite",
                max_series=config.get("max_series", 10000),
                per_metric=config.get("per_metric", 1000),
            )
            alerts = Alerts(store)
            if args.rules_file:
                alerts.load_rules(args.rules_file)
        elif args.rules_file:
            parser.error("rule files require an api/storage/alerts/archive role")
        if args.role == "archive":
            if args.start is None or args.end is None or not args.output:
                parser.error("archive requires --start, --end and --output")
            print(json.dumps(store.archive(args.output, args.start, args.end)))
            return
        if args.role == "storage":
            queue = _queue(config)
            if callable(getattr(queue, "close", None)):
                stack.callback(queue.close)
            try:
                storage_loop(store, queue, stop)
            except KeyboardInterrupt:
                stop.set()
            return
        if args.role == "alerts":
            provider = WebhookProvider(config.get("destinations", {}))

            def evaluate_and_notify():
                alerts.evaluate()
                for _ in range(100):
                    if stop.is_set() or not alerts.notify_once(provider):
                        break

            worker = Worker(evaluate_and_notify, args.interval, name="metrics-alerts")
            stack.callback(worker.close)
            worker.start()
            try:
                while not stop.wait(0.2):
                    pass
            except KeyboardInterrupt:
                stop.set()
            return

        workers = {}
        services = {}
        if args.role == "exporter":
            exporter = Exporter(args.instance)
            stack.callback(exporter.close)
            services["exporter"] = exporter
        elif args.role == "api":
            services.update(store=store, alerts=alerts)
        else:
            from .discovery import ScrapeScheduler
            from .pipeline import Dispatcher

            collector = Collector(
                root / "collector.sqlite",
                max_bytes=config.get("spool_bytes", 16 * 1024 * 1024),
                partitions=config.get("queue_partitions", 1),
                partition_by=config.get("partition_by", "metric"),
            )
            queue = _queue(config)
            if callable(getattr(queue, "close", None)):
                stack.callback(queue.close)
            dispatcher = Dispatcher(
                collector, queue, workers=config.get("dispatch_workers", 4)
            )
            stack.callback(dispatcher.close)
            scheduler = ScrapeScheduler(
                collector,
                args.targets,
                workers=config.get("scrape_workers", 4),
                collector_id=config.get("collector_id", "local"),
                collectors=config.get("collectors", ("local",)),
                default_interval=args.interval,
            )
            stack.callback(scheduler.close)
            workers = {
                "dispatcher": Worker(
                    dispatcher.run_once, 0.2, name="metrics-dispatch", continuous=True
                ),
                "discovery": Worker(scheduler.run_once, 0.2, name="metrics-discovery"),
            }
            for worker in workers.values():
                stack.callback(worker.close)
            services["collector"] = collector
        httpd = server(
            args.host,
            args.port,
            config,
            **services,
            workers=workers,
            concurrency=args.concurrency,
            request_timeout=args.request_timeout,
        )
        stack.callback(httpd.server_close)
        for worker in workers.values():
            worker.start()
        print(
            f"{args.role} listening on http://{args.host}:{httpd.server_port}",
            flush=True,
        )
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            stop.set()


if __name__ == "__main__":
    main()
