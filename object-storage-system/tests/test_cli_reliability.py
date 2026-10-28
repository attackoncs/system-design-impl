"""Config validation and API resource drain regressions."""

from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from object_storage.cli import load_config, serve_role


def valid_config():
    return {
        "users": {"alice": "a" * 48},
        "internal_token": "b" * 48,
        "admin_token": "c" * 48,
        "continuation_secret": "d" * 48,
        "data_urls": [
            "http://127.0.0.1:9853",
            "http://127.0.0.1:9854",
            "http://127.0.0.1:9855",
        ],
        "metadata_urls": ["http://127.0.0.1:9851", "http://127.0.0.1:9852"],
        "write_quorum": 2,
    }


class CLIConfigTests(unittest.TestCase):
    def load(self, value, *, encoded=False):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(value if encoded else json.dumps(value), encoding="utf-8")
            return load_config(path)

    def test_valid_config_normalizes_loopback_aliases(self):
        config = valid_config()
        config["data_urls"][0] = "http://localhost:9853/"
        self.assertEqual(self.load(config)["data_urls"][0], "http://127.0.0.1:9853")

    def test_alias_duplicate_endpoints_cannot_count_as_distinct_replicas(self):
        config = valid_config()
        config["data_urls"][1] = "http://localhost:9853/"
        with self.assertRaises(ValueError):
            self.load(config)

    def test_distinct_credentials_are_ascii_private_values(self):
        for token in ("x" * 20, "x" * 24 + "\n", "x" * 24 + "中文", "x " * 24, True):
            with self.subTest(token=token):
                config = valid_config()
                config["internal_token"] = token
                with self.assertRaises(ValueError):
                    self.load(config)
        config = valid_config()
        config["admin_token"] = config["users"]["alice"]
        with self.assertRaises(ValueError):
            self.load(config)

    def test_external_credentials_query_and_base_path_endpoints_fail(self):
        for url in (
            "http://example.com:80",
            "http://127.0.0.1:9853/path",
            "http://secret@127.0.0.1:9853",
            "http://127.0.0.1:9853?q=x",
            "https://127.0.0.1:9853",
            "http://127.0.0.1:0",
        ):
            with self.subTest(url=url):
                config = valid_config()
                config["data_urls"][0] = url
                with self.assertRaises(ValueError):
                    self.load(config)

    def test_duplicate_json_nonfinite_and_quorum_type_fail(self):
        for raw in ('{"users":{},"users":{}}', '{"x":NaN}', '{"x":1e999}'):
            with self.assertRaises(ValueError):
                self.load(raw, encoded=True)
        config = valid_config()
        config["write_quorum"] = True
        with self.assertRaises(ValueError):
            self.load(config)


class CLILifecycleTests(unittest.TestCase):
    def args(self, root):
        return SimpleNamespace(
            role="api", root=root, host="127.0.0.1", port=12345, index=0
        )

    def test_bind_failure_closes_owned_service(self):
        service = Mock()
        with (
            tempfile.TemporaryDirectory() as root,
            patch("object_storage.cli.Service", return_value=service),
            patch("object_storage.cli.Catalog"),
            patch("object_storage.cli.server", side_effect=OSError("bind")),
        ):
            with self.assertRaises(OSError):
                serve_role(self.args(root), valid_config())
        service.close.assert_called_once_with()

    def test_serving_failure_drains_server_before_service(self):
        operations = []
        service = Mock()
        service.close.side_effect = lambda: operations.append("service")
        peer = Mock()
        peer.serve_forever.side_effect = KeyboardInterrupt
        peer.server_close.side_effect = lambda: operations.append("server")
        with (
            tempfile.TemporaryDirectory() as root,
            patch("object_storage.cli.Service", return_value=service),
            patch("object_storage.cli.Catalog"),
            patch("object_storage.cli.server", return_value=peer),
        ):
            with self.assertRaises(KeyboardInterrupt):
                serve_role(self.args(root), valid_config())
        self.assertEqual(operations, ["server", "service"])

    def test_close_failure_still_closes_service(self):
        service = Mock()
        peer = Mock()
        peer.server_close.side_effect = OSError("close")
        with (
            tempfile.TemporaryDirectory() as root,
            patch("object_storage.cli.Service", return_value=service),
            patch("object_storage.cli.Catalog"),
            patch("object_storage.cli.server", return_value=peer),
        ):
            with self.assertRaises(OSError):
                serve_role(self.args(root), valid_config())
        service.close.assert_called_once_with()
