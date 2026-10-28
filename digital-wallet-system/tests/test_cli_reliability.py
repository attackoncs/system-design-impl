import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from helpers_review import configuration
from digital_wallet.cli import load_config, serve_role


class CLIConfigTests(unittest.TestCase):
    def config(self, value):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
            return load_config(path)

    def test_private_local_config_and_canonical_endpoint_aliases(self):
        value = configuration()
        self.assertEqual(self.config(value), value)
        value["replicas"][0] = "http://localhost:9880"
        with self.assertRaises(ValueError):
            self.config(value)

    def test_nonlocal_credentials_paths_query_fragment_and_duplicate_config(self):
        for url in (
            "http://example.com:9880",
            "http://user:pass@127.0.0.1:9880",
            "http://127.0.0.1:9880/path",
            "http://127.0.0.1:9880?query=1",
            "http://127.0.0.1:9880#part",
            " http://127.0.0.1:9880",
        ):
            value = configuration()
            value["coordinator"] = url
            with self.assertRaises(ValueError):
                self.config(value)
        for raw in ('{"a":1,"a":2}', '{"amount":NaN}'):
            with self.assertRaises((ValueError, KeyError)):
                self.config(raw)

    def test_boolean_epoch_and_nonascii_or_reused_credentials_fail(self):
        for key, replacement in (
            ("epoch", True),
            ("internal_token", "密" * 24),
            ("admin_token", "a" * 300),
        ):
            value = configuration()
            value[key] = replacement
            with self.assertRaises(ValueError):
                self.config(value)
        value = configuration()
        value["admin_token"] = value["internal_token"]
        with self.assertRaises(ValueError):
            self.config(value)

    def test_bind_failure_closes_owned_coordinator_runtime(self):
        args = SimpleNamespace(role="coordinator", root="unused", index=0)
        with (
            patch("digital_wallet.cli.Coordinator") as store,
            patch("digital_wallet.cli.server", side_effect=OSError("busy")),
        ):
            with self.assertRaises(OSError):
                serve_role(args, configuration())
            store.return_value.close.assert_called_once()

    def test_listener_close_failure_still_closes_coordinator_runtime(self):
        args = SimpleNamespace(role="coordinator", root="unused", index=0)
        with (
            patch("digital_wallet.cli.Coordinator") as store,
            patch("digital_wallet.cli.server") as listener,
        ):
            listener.return_value.serve_forever.side_effect = RuntimeError("stopped")
            listener.return_value.server_close.side_effect = OSError("close failed")
            with self.assertRaises(OSError):
                serve_role(args, configuration())
            store.return_value.close.assert_called_once()
