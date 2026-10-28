from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.cli import main
from metrics_monitor.lifecycle import Worker


class WorkerTests(unittest.TestCase):
    def test_failure_is_visible_and_retry_recovers(self):
        success = threading.Event()
        count = [0]

        def action():
            count[0] += 1
            if count[0] == 1:
                raise RuntimeError("private payload must not be logged")
            success.set()

        worker = Worker(action, 0.02)
        try:
            with self.assertLogs("metrics_monitor.lifecycle", level="WARNING") as logs:
                worker.start()
                self.assertTrue(success.wait(1))
            self.assertNotIn("private payload", "\n".join(logs.output))
            self.assertIsNone(worker.last_error)
        finally:
            worker.close()

    def test_close_drains_inflight_and_prevents_next_call(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def action():
            calls.append(1)
            entered.set()
            release.wait(3)

        worker = Worker(action, 0.01).start()
        self.assertTrue(entered.wait(1))
        closer = threading.Thread(target=worker.close)
        closer.start()
        try:
            time.sleep(0.05)
            self.assertTrue(closer.is_alive())
        finally:
            release.set()
            closer.join(2)
            worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(calls, [1])

    def test_close_wakes_idle_and_disallows_restart(self):
        worker = Worker(lambda: None, 3600).start()
        worker.close()
        self.assertFalse(worker.thread.is_alive())
        with self.assertRaises(RuntimeError):
            worker.start()

    def test_internal_action_error_is_visible(self):
        class Action:
            last_error = "pending transport retry"

            def run_once(self):
                return False

        worker = Worker(Action().run_once, 1)
        self.assertEqual(worker.last_error, "pending transport retry")
        worker.close()

    def test_invalid_inputs(self):
        for interval in (0, True, float("nan"), 10**10000):
            with self.assertRaises(ValueError):
                Worker(lambda: None, interval)
        with self.assertRaises(ValueError):
            Worker(None, 1)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "private.json"
        self.config.write_text(
            json.dumps(
                {
                    "queue_url": "http://unused",
                    "queue_token": "private-token",
                    "read_token": "private-token",
                }
            )
        )

    def args(self, role):
        return [
            role,
            "--root",
            str(self.root / "data"),
            "--config",
            str(self.config),
            "--port",
            "0",
        ]

    def test_exporter_bind_failure_releases_tracer(self):
        with (
            patch("metrics_monitor.cli.Exporter") as exporter,
            patch("metrics_monitor.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                main(self.args("exporter"))
        exporter.return_value.close.assert_called_once()

    def test_collector_bind_failure_starts_no_worker_and_closes_helpers(self):
        with (
            patch("metrics_monitor.cli.Collector"),
            patch("metrics_monitor.cli.QueueAdapter") as queue,
            patch("metrics_monitor.pipeline.Dispatcher") as dispatcher,
            patch("metrics_monitor.discovery.ScrapeScheduler") as scheduler,
            patch("metrics_monitor.cli.Worker") as worker,
            patch("metrics_monitor.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                main(self.args("collector"))
            worker.return_value.start.assert_not_called()
            self.assertEqual(worker.return_value.close.call_count, 2)
            dispatcher.return_value.close.assert_called_once()
            scheduler.return_value.close.assert_called_once()
            queue.return_value.close.assert_called_once()

    def test_server_drains_before_workers_and_helpers(self):
        order = []
        dispatcher, scheduler, queue = Mock(), Mock(), Mock()
        first, second, httpd = Mock(), Mock(), Mock()
        for name, action in (
            ("dispatch-start", first.start),
            ("scrape-start", second.start),
            ("dispatch-close", first.close),
            ("scrape-close", second.close),
            ("server-close", httpd.server_close),
            ("dispatcher-close", dispatcher.close),
            ("scheduler-close", scheduler.close),
            ("queue-close", queue.close),
        ):
            action.side_effect = lambda label=name: order.append(label)
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with (
            patch("metrics_monitor.cli.Collector"),
            patch("metrics_monitor.cli.QueueAdapter", return_value=queue),
            patch("metrics_monitor.pipeline.Dispatcher", return_value=dispatcher),
            patch("metrics_monitor.discovery.ScrapeScheduler", return_value=scheduler),
            patch("metrics_monitor.cli.Worker", side_effect=[first, second]),
            patch("metrics_monitor.cli.server", return_value=httpd),
            patch("builtins.print"),
        ):
            main(self.args("collector"))
        self.assertEqual(
            order,
            [
                "dispatch-start",
                "scrape-start",
                "server-close",
                "scrape-close",
                "dispatch-close",
                "scheduler-close",
                "dispatcher-close",
                "queue-close",
            ],
        )

    def test_rule_file_loaded_before_binding(self):
        rules = self.root / "rules.json"
        rules.write_text("[]")
        order = []
        alerts, httpd = Mock(), Mock()
        alerts.load_rules.side_effect = lambda path: order.append(("rules", path))
        httpd.serve_forever.side_effect = KeyboardInterrupt

        def bind(*args, **kwargs):
            order.append(("bind", None))
            return httpd

        with (
            patch("metrics_monitor.cli.Store"),
            patch("metrics_monitor.cli.Alerts", return_value=alerts),
            patch("metrics_monitor.cli.server", side_effect=bind),
            patch("builtins.print"),
        ):
            main(self.args("api") + ["--rules-file", str(rules)])
        self.assertEqual(order, [("rules", str(rules)), ("bind", None)])
        httpd.server_close.assert_called_once()

    def test_invalid_limits_fail_before_constructing_service(self):
        with patch("metrics_monitor.cli.Exporter") as constructor:
            for options in (
                ["--interval", "nan"],
                ["--interval", "inf"],
                ["--concurrency", "0"],
                ["--request-timeout", "inf"],
                ["--port", "65536"],
            ):
                with (
                    self.subTest(options=options),
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    main(self.args("exporter") + options)
            self.config.write_text('{"a":1,"a":2}')
            with self.assertRaises(ValueError):
                main(self.args("exporter"))
            constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
