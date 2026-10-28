import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from helpers_review import configuration
from scale_from_zero.cli import load_config, serve_role


class CLIConfigTests(unittest.TestCase):
    def config(self, value):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
            return load_config(path)

    def test_all_stages_and_configured_three_web_pool(self):
        for stage in ("single", "balanced", "sharded"):
            value = configuration(stage, web_nodes=3)
            self.assertEqual(self.config(value), value)

    def test_canonical_service_aliases_and_mismatched_references_are_rejected(self):
        value = configuration("balanced")
        value["services"]["web0"] = value["services"]["cache"].replace(
            "127.0.0.1", "localhost"
        )
        with self.assertRaises(ValueError):
            self.config(value)
        value = configuration("balanced")
        value["entry"] = value["cache"]
        with self.assertRaises(ValueError):
            self.config(value)

    def test_unknown_incomplete_and_nonlocal_topologies_are_rejected(self):
        for mutation in (
            lambda value: value["services"].pop("primary0"),
            lambda value: value["primaries"].clear(),
            lambda value: value["services"].update(web0="http://example.com:9999"),
        ):
            value = configuration("balanced")
            mutation(value)
            with self.assertRaises(ValueError):
                self.config(value)

    def test_duplicate_json_nonfinite_and_nonascii_credentials_fail(self):
        for value in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                self.config(value)
        value = configuration("single")
        value["admin_token"] = "\u5bc6" * 24
        with self.assertRaises(ValueError):
            self.config(value)

    def test_failed_bind_starts_no_replication_worker(self):
        with (
            patch("scale_from_zero.cli.Store"),
            patch("scale_from_zero.cli.PeriodicWorker") as worker,
            patch("scale_from_zero.cli.server", side_effect=OSError("busy")),
        ):
            with self.assertRaises(OSError):
                serve_role(
                    SimpleNamespace(node="replica0", root="unused"),
                    configuration("balanced"),
                )
            worker.return_value.start.assert_not_called()

    def test_listener_close_failure_still_stops_and_joins_replication_worker(self):
        with (
            patch("scale_from_zero.cli.Store"),
            patch("scale_from_zero.cli.PeriodicWorker") as worker,
            patch("scale_from_zero.cli.server") as listener,
        ):
            listener.return_value.serve_forever.side_effect = RuntimeError("stopped")
            listener.return_value.server_close.side_effect = OSError("close failed")
            with self.assertRaises(OSError):
                serve_role(
                    SimpleNamespace(node="replica0", root="unused"),
                    configuration("balanced"),
                )
            worker.return_value.stop.assert_called_once()
            worker.return_value.close.assert_called_once()
