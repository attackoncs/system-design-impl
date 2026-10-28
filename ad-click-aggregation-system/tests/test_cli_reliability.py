"""Configuration and owned worker lifecycle with controlled application I/O."""

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
from ad_click.cli import load_config, main
from ad_click.lifecycle import WorkerLoop


class WorkerLoopTests(unittest.TestCase):
    def store(self):
        store = Mock()
        store.partition = 0
        store.status.return_value = {"seq": 0}
        return store

    def test_apply_failure_is_visible_and_durable_cursor_is_retried(self):
        store = self.store()
        recovered = threading.Event()
        store.apply.side_effect = [RuntimeError("private input body"), None]
        store.idle.side_effect = recovered.set
        loop = WorkerLoop({}, store)
        with patch("ad_click.service.log_page", return_value={"entries": []}) as page:
            try:
                with self.assertLogs("ad_click.lifecycle", level="WARNING") as logs:
                    loop.start()
                    self.assertTrue(recovered.wait(1))
                    loop.close()
                self.assertIsNone(loop.error)
                self.assertEqual([call.args[2] for call in page.call_args_list], [0, 0])
                self.assertNotIn("private input", "\n".join(logs.output))
            finally:
                loop.close()

    def test_close_waits_for_accepted_poll_and_prevents_next_attempt(self):
        entered, release = threading.Event(), threading.Event()

        def page(*args):
            entered.set()
            release.wait(3)
            return {"entries": []}

        loop = WorkerLoop({}, self.store())
        with patch("ad_click.service.log_page", side_effect=page) as call:
            loop.start()
            self.assertTrue(entered.wait(1))
            closer = threading.Thread(target=loop.close)
            closer.start()
            try:
                time.sleep(0.05)
                self.assertTrue(closer.is_alive())
            finally:
                release.set()
                closer.join(2)
                loop.close()
            self.assertFalse(loop.thread.is_alive())
            self.assertEqual(call.call_count, 1)

    def test_close_before_start_prevents_worker_leak(self):
        loop = WorkerLoop({}, self.store())
        loop.close()
        with self.assertRaises(RuntimeError):
            loop.start()
        self.assertFalse(loop.thread.is_alive())


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "private.json"
        self.config = {
            role + "_token": role[0] * 32
            for role in ("write", "read", "internal", "admin")
        }
        self.config.update(
            partitions=1,
            ingress_url="http://127.0.0.1:8000",
            worker_urls=["http://127.0.0.1:8001"],
        )
        self.path.write_text(json.dumps(self.config))
        self.args = [
            "worker",
            "--config",
            str(self.path),
            "--root",
            str(self.root / "data"),
            "--port",
            "0",
        ]

    def test_bind_failure_starts_no_worker_and_closes_it(self):
        with (
            patch("ad_click.cli.Aggregator"),
            patch("ad_click.cli.WorkerLoop") as loop,
            patch("ad_click.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                main(self.args)
            loop.return_value.start.assert_not_called()
            loop.return_value.close.assert_called_once()

    def test_bind_then_start_and_server_drain_before_worker_close(self):
        order = []
        loop, httpd = Mock(), Mock()
        loop.start.side_effect = lambda: order.append("worker-start")
        loop.close.side_effect = lambda: order.append("worker-close")
        httpd.server_close.side_effect = lambda: order.append("server-close")
        httpd.serve_forever.side_effect = KeyboardInterrupt

        def bind(*args, **kwargs):
            order.append("bind")
            return httpd

        with (
            patch("ad_click.cli.Aggregator"),
            patch("ad_click.cli.WorkerLoop", return_value=loop),
            patch("ad_click.cli.server", side_effect=bind),
        ):
            main(self.args)
        self.assertEqual(
            order, ["bind", "worker-start", "server-close", "worker-close"]
        )

    def test_bad_http_limits_fail_before_store_creation(self):
        with patch("ad_click.cli.Aggregator") as constructor:
            for options in (
                ["--concurrency", "0"],
                ["--port", "65536"],
                ["--request-timeout", "nan"],
                ["--request-timeout", "inf"],
            ):
                with (
                    self.subTest(options=options),
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    main(self.args + options)
            constructor.assert_not_called()

    def test_config_duplicate_fields_credentials_and_topology_validation(self):
        for change in (
            {"worker_urls": ["http://u:p@localhost"]},
            {"worker_urls": "http://localhost"},
            {"ingress_url": "http://localhost/path"},
            {"lateness": True},
            {"max_events": 0},
            {"read_token": self.config["write_token"]},
        ):
            with self.subTest(change=change):
                self.path.write_text(json.dumps({**self.config, **change}))
                with self.assertRaises(ValueError):
                    load_config(self.path)
        self.path.write_text('{"partitions":1,"partitions":2}')
        with self.assertRaises(ValueError):
            load_config(self.path)


if __name__ == "__main__":
    unittest.main()
