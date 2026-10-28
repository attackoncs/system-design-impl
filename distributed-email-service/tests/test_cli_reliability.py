"""Listener failures and shutdown retain ownership of background operations."""

import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from distributed_email import cli


def configuration():
    return {
        "users": {"alice": "a" * 48},
        "admin_token": "b" * 48,
        "internal_token": "c" * 48,
        "smtp_token": "d" * 48,
        "primary_urls": ["http://127.0.0.1:9801"],
        "replica_urls": ["http://127.0.0.1:9802"],
        "object_url": "http://127.0.0.1:9803",
        "smtp_port": 9804,
    }


class CLIConfigTests(unittest.TestCase):
    def load(self, config):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                config if isinstance(config, str) else json.dumps(config),
                encoding="utf-8",
            )
            return cli.load_config(path)

    def test_valid_config_is_normalized_without_external_endpoints(self):
        config = configuration()
        config["primary_urls"][0] += "/"
        self.assertEqual(self.load(config)["primary_urls"], ["http://127.0.0.1:9801"])

    def test_duplicate_json_and_nonfinite_values_fail(self):
        with self.assertRaises(ValueError):
            self.load('{"users":{},"users":{}}')
        with self.assertRaises(ValueError):
            self.load('{"x":NaN}')

    def test_urls_reject_external_proxy_credentials_and_base_queries(self):
        for url in (
            "http://example.com",
            "http://127.0.0.1/x",
            "http://127.0.0.1?x=1",
            "http://u:p@127.0.0.1:9800",
            "https://127.0.0.1:9800",
            "http://127.0.0.1:0",
        ):
            config = configuration()
            config["object_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(config)

    def test_role_credentials_require_ascii_distinct_private_values(self):
        for token in ("x", "汉" * 48, "a" * 48, "x" * 48 + "\n"):
            config = configuration()
            config["internal_token"] = token
            with self.subTest(token=token), self.assertRaises(ValueError):
                self.load(config)

    def test_duplicate_object_mailbox_endpoint_fails(self):
        config = configuration()
        config["object_url"] = config["primary_urls"][0] + "/"
        with self.assertRaises(ValueError):
            self.load(config)


class CLILifecycleTests(unittest.TestCase):
    def args(self, role):
        return SimpleNamespace(
            role=role, shard=0, root="unused", host="127.0.0.1", port=9800
        )

    def test_replica_http_bind_failure_starts_no_replication(self):
        with (
            patch.object(cli, "MailStore"),
            patch.object(cli, "replicate") as replicate,
            patch.object(cli, "server", side_effect=OSError("address busy")),
        ):
            with self.assertRaises(OSError):
                cli.serve_role(self.args("replica"), configuration())
            replicate.assert_not_called()

    def test_smtp_http_bind_failure_closes_unstarted_wire_listener(self):
        smtp = Mock()
        with (
            patch.object(cli, "Spool"),
            patch.object(cli, "smtp_server", return_value=smtp),
            patch.object(cli, "server", side_effect=OSError("address busy")),
        ):
            with self.assertRaises(OSError):
                cli.serve_role(self.args("smtp"), configuration())
        smtp.server_close.assert_called_once_with()
        smtp.shutdown.assert_not_called()
        smtp.serve_forever.assert_not_called()

    def test_serving_failure_stops_and_joins_replica_thread(self):
        drained = threading.Event()

        def replicate(_config, _store, stop, _state):
            stop.wait(2)
            drained.set()

        instance = Mock()
        instance.serve_forever.side_effect = RuntimeError("serve failed")
        with (
            patch.object(cli, "MailStore"),
            patch.object(cli, "replicate", side_effect=replicate),
            patch.object(cli, "server", return_value=instance),
        ):
            with self.assertRaisesRegex(RuntimeError, "serve failed"):
                cli.serve_role(self.args("replica"), configuration())
        self.assertTrue(drained.is_set())
        instance.server_close.assert_called_once_with()
        self.assertFalse(
            any(thread.name == "email-replication" for thread in threading.enumerate())
        )

    def test_listener_close_failure_still_drains_owned_background(self):
        drained = threading.Event()

        def replicate(_config, _store, stop, _state):
            stop.wait(2)
            drained.set()

        instance = Mock()
        instance.server_close.side_effect = RuntimeError("close failed")
        with (
            patch.object(cli, "MailStore"),
            patch.object(cli, "replicate", side_effect=replicate),
            patch.object(cli, "server", return_value=instance),
        ):
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                cli.serve_role(self.args("replica"), configuration())
        self.assertTrue(drained.is_set())

    def test_worker_stop_is_set_even_on_keyboard_interrupt(self):
        received = []

        def sender(_config, _shard, *, stop):
            received.append(stop)
            raise KeyboardInterrupt

        with patch.object(cli, "run_sender", side_effect=sender):
            with self.assertRaises(KeyboardInterrupt):
                cli.serve_role(self.args("worker"), configuration())
        self.assertTrue(received[0].is_set())


if __name__ == "__main__":
    unittest.main()
