import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_http_reliability import configuration
from payment_system.cli import load_config, serve_role


class CLIConfigTests(unittest.TestCase):
    def config(self, value):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
            return load_config(path)

    def test_private_local_config_and_endpoint_aliases(self):
        config = configuration([9870, 9871, 9872])
        self.assertEqual(self.config(config)["api_url"], config["api_url"])
        config["provider_url"] = "http://localhost:9870"
        with self.assertRaises(ValueError):
            self.config(config)

    def test_nonlocal_credentialed_query_and_ambiguous_config_rejected(self):
        for url in (
            "http://example.com",
            "http://user:pass@127.0.0.1:9870",
            "http://127.0.0.1/path",
            "http://127.0.0.1?query=1",
        ):
            config = configuration([9870, 9871, 9872])
            config["api_url"] = url
            with self.assertRaises(ValueError):
                self.config(config)
        for raw in ('{"a":1,"a":2}', '{"amount":NaN}'):
            with self.assertRaises((ValueError, KeyError)):
                self.config(raw)

    def test_bool_worker_count_and_nonascii_credentials_fail(self):
        config = configuration([9870, 9871, 9872])
        config["worker_concurrency"] = True
        with self.assertRaises(ValueError):
            self.config(config)
        del config["worker_concurrency"]
        config["webhook_secret"] = "密" * 24
        with self.assertRaises(ValueError):
            self.config(config)

    def test_binding_failure_starts_no_psp_dispatch_thread(self):
        args = SimpleNamespace(
            role="provider", root="unused", host="127.0.0.1", port=9871
        )
        with (
            patch("payment_system.cli.MockPSP") as store,
            patch("payment_system.cli.server", side_effect=OSError("busy")),
            patch("payment_system.cli.threading.Thread") as thread,
        ):
            with self.assertRaises(OSError):
                serve_role(args, configuration([9870, 9871, 9872]))
            thread.assert_not_called()
            store.return_value.dispatch_once.assert_not_called()

    def test_serving_failure_stops_and_joins_dispatch_even_if_close_fails(self):
        entered, observed = threading.Event(), []

        class Provider:
            def dispatch_once(self, *, deadline, cancel):
                entered.set()
                cancel.wait(1)
                observed.append(cancel.is_set())
                return False

        class Listener:
            def serve_forever(self, **kwargs):
                if not entered.wait(1):
                    raise AssertionError("dispatcher did not start")
                raise RuntimeError("stopped")

            def server_close(self):
                raise OSError("close failed")

        args = SimpleNamespace(
            role="provider", root="unused", host="127.0.0.1", port=9871
        )
        with (
            patch("payment_system.cli.MockPSP", return_value=Provider()),
            patch("payment_system.cli.server", return_value=Listener()),
        ):
            with self.assertRaises(OSError):
                serve_role(args, configuration([9870, 9871, 9872]))
        self.assertEqual(observed, [True])
        self.assertFalse(
            any(
                thread.name == "simulated-psp-callbacks"
                for thread in threading.enumerate()
            )
        )
