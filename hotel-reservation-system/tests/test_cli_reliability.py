"""Configuration and periodic-worker ownership with controlled application I/O."""

from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hotel_reservation.cli import load_config, main
from hotel_reservation.lifecycle import Worker


class WorkerTests(unittest.TestCase):
    def test_failure_retries_without_exposing_private_error(self):
        completed = threading.Event()
        count = [0]

        def action():
            count[0] += 1
            if count[0] == 1:
                raise OSError("private reservation contents")
            completed.set()

        worker = Worker(action, 0.01)
        try:
            with self.assertLogs(
                "hotel_reservation.lifecycle", level="WARNING"
            ) as logs:
                worker.start()
                self.assertTrue(completed.wait(1))
            self.assertNotIn("private reservation contents", "\n".join(logs.output))
            self.assertIsNone(worker.last_error)
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())

    def test_close_drains_accepted_action_and_wakes_wait(self):
        entered, release, closed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def action():
            entered.set()
            release.wait(2)

        worker = Worker(action, 30).start()
        closer = threading.Thread(target=lambda: (worker.close(), closed.set()))
        try:
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertFalse(closed.wait(0.05))
        finally:
            release.set()
            closer.join(2)
            worker.close()
        self.assertTrue(closed.is_set())
        self.assertFalse(worker.thread.is_alive())

    def test_close_before_start_prevents_late_worker(self):
        worker = Worker(lambda: None, 1)
        worker.close()
        with self.assertRaises(RuntimeError):
            worker.start()
        self.assertFalse(worker.thread.is_alive())


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "private.json"
        self.config = {
            role + "_token": role[0] * 32 for role in ("admin", "internal", "payment")
        }
        self.config.update(
            users={"alice": "u" * 32},
            shard_urls=["http://127.0.0.1:8000"],
            payment_url="http://127.0.0.1:8001",
        )
        self.path.write_text(json.dumps(self.config))
        self.args = [
            "shard",
            "--config",
            str(self.path),
            "--root",
            str(self.root / "data"),
            "--port",
            "0",
        ]

    def test_invalid_config_urls_credentials_and_nonfinite_json(self):
        self.assertEqual(load_config(self.path)["users"], {"alice": "u" * 32})
        for body in (
            {**self.config, "payment_url": "http://user:secret@host"},
            {**self.config, "shard_urls": ["http://host/path"]},
            {**self.config, "shard_urls": ["http://host", "http://host"]},
            {**self.config, "users": {"alice": self.config["admin_token"]}},
            {**self.config, "calendar_interval": float("nan")},
        ):
            self.path.write_text(json.dumps(body))
            with self.assertRaises(ValueError):
                load_config(self.path)
        self.path.write_text('{"admin_token":"x","admin_token":"y"}')
        with self.assertRaises(ValueError):
            load_config(self.path)

    def test_bind_failure_does_not_start_maintenance(self):
        with (
            patch("hotel_reservation.cli.Store"),
            patch("hotel_reservation.calendar_policy.CalendarPolicies"),
            patch("hotel_reservation.cli.Worker") as worker,
            patch("hotel_reservation.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                main(self.args)
            worker.return_value.start.assert_not_called()
            worker.return_value.close.assert_called_once()

    def test_bind_start_then_handler_drain_before_maintenance_close(self):
        order = []
        worker, httpd = Mock(), Mock()
        worker.start.side_effect = lambda: order.append("worker-start")
        worker.close.side_effect = lambda: order.append("worker-close")
        httpd.serve_forever.side_effect = lambda **kwargs: order.append("serve")
        httpd.server_close.side_effect = lambda: order.append("http-close")

        def bind(*args, **kwargs):
            order.append("bind")
            self.assertIn("calendar", kwargs)
            self.assertIn("workers", kwargs)
            return httpd

        with (
            patch("hotel_reservation.cli.Store"),
            patch("hotel_reservation.calendar_policy.CalendarPolicies"),
            patch("hotel_reservation.cli.Worker", return_value=worker),
            patch("hotel_reservation.cli.server", side_effect=bind),
        ):
            main(self.args)
        self.assertEqual(
            order, ["bind", "worker-start", "serve", "http-close", "worker-close"]
        )

    def test_worker_role_and_invalid_runtime_limits(self):
        with patch("hotel_reservation.cli.run") as run:
            main(["worker", "--config", str(self.path)])
            run.assert_called_once()
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([*self.args, "--request-timeout", "nan"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            main([*self.args, "--concurrency", "0"])


if __name__ == "__main__":
    unittest.main()
