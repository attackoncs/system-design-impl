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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from message_queue.cli import main
from message_queue.lifecycle import Maintenance


class MaintenanceTests(unittest.TestCase):
    def test_failure_is_visible_and_retry_recovers(self):
        succeeded = threading.Event()
        count = [0]

        def action():
            count[0] += 1
            if count[0] == 1:
                raise RuntimeError("do not log private payload")
            succeeded.set()

        worker = Maintenance(action, interval=0.02)
        try:
            with self.assertLogs("message_queue.lifecycle", level="WARNING") as logs:
                worker.start()
                self.assertTrue(succeeded.wait(1))
            self.assertNotIn("private payload", "\n".join(logs.output))
            self.assertIsNone(worker.last_error)
            self.assertGreaterEqual(count[0], 2)
        finally:
            worker.close()

    def test_close_drains_inflight_and_no_next_iteration(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def action():
            calls.append(1)
            entered.set()
            release.wait(2)

        worker = Maintenance(action, interval=0.01).start()
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
        self.assertFalse(closer.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertFalse(worker.thread.is_alive())

    def test_close_wakes_idle_and_prevents_restart(self):
        worker = Maintenance(lambda: None, interval=3600).start()
        worker.close()
        self.assertFalse(worker.thread.is_alive())
        with self.assertRaises(RuntimeError):
            worker.start()

    def test_invalid_configuration(self):
        for interval in (0, -1, True, float("nan"), float("inf"), 10**10000):
            with self.assertRaises(ValueError):
                Maintenance(lambda: None, interval)
        with self.assertRaises(ValueError):
            Maintenance(None)


class CLILifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "private.json"
        self.config.write_text(
            json.dumps(
                {
                    "cluster_token": "cluster-private-token",
                    "client_token": "client-private-token",
                    "brokers": {"b0": "http://localhost:8001"},
                }
            ),
            encoding="utf-8",
        )
        self.argv = [
            "coordinator",
            "--root",
            str(self.root / "data"),
            "--config",
            str(self.config),
            "--port",
            "0",
        ]

    def test_bind_failure_never_starts_worker_and_closes_it(self):
        with (
            patch("message_queue.cli.Coordinator"),
            patch("message_queue.cli.Maintenance") as worker,
            patch("message_queue.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                main(self.argv)
            worker.return_value.start.assert_not_called()
            worker.return_value.close.assert_called_once()

    def test_server_drains_before_worker_close(self):
        calls = []
        worker, httpd = Mock(), Mock()
        httpd.server_port = 8123
        httpd.serve_forever.side_effect = KeyboardInterrupt
        worker.start.side_effect = lambda: calls.append("start")
        worker.close.side_effect = lambda: calls.append("worker-close")
        httpd.server_close.side_effect = lambda: calls.append("server-close")
        with (
            patch("message_queue.cli.Coordinator"),
            patch("message_queue.cli.Maintenance", return_value=worker),
            patch("message_queue.cli.server", return_value=httpd),
            patch("builtins.print"),
        ):
            main(self.argv)
        self.assertEqual(calls, ["start", "server-close", "worker-close"])

    def test_bad_limits_and_duplicate_config_fail_before_service_creation(self):
        with patch("message_queue.cli.Coordinator") as constructor:
            for extra in (
                ["--lease-seconds", "nan"],
                ["--request-timeout", "inf"],
                ["--concurrency", "0"],
                ["--maintenance-interval", "-1"],
            ):
                with self.subTest(extra=extra), redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit):
                        main(self.argv + extra)
            self.config.write_text(
                '{"cluster_token":"abcdefgh","cluster_token":"changed"}'
            )
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                main(self.argv)
            constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
