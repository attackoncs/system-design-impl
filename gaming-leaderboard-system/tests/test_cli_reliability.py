"""Configuration isolation and owned projection lifecycle."""

import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gaming_leaderboard.cli import load_config, serve_role


def configuration():
    value = {
        kind + "_token": char * 48
        for kind, char in (
            ("admin", "a"),
            ("read", "r"),
            ("game", "g"),
            ("internal", "i"),
        )
    }
    return {
        **value,
        "redis_password": "p" * 48,
        "namespace": "test",
        "authority_url": "http://127.0.0.1:9000",
        "projection_urls": ["http://127.0.0.1:9001"],
        "redis_nodes": [
            [{"host": "127.0.0.1", "port": 9002}, {"host": "127.0.0.1", "port": 9003}]
        ],
        "late_grace": 172800,
    }


class CLIConfigTests(unittest.TestCase):
    def load(self, value=None, raw=None):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            path.write_text(
                json.dumps(configuration() if value is None else value)
                if raw is None
                else raw,
                encoding="utf-8",
            )
            return load_config(path)

    def test_valid_local_configuration_is_normalized(self):
        value = configuration()
        value["authority_url"] = "http://localhost:9000/"
        value["redis_nodes"][0][0]["host"] = "localhost"
        result = self.load(value)
        self.assertEqual(result["authority_url"], "http://127.0.0.1:9000")
        self.assertEqual(result["redis_nodes"][0][0]["host"], "127.0.0.1")

    def test_nonlocal_credentialed_and_ambiguous_urls_fail(self):
        for url in (
            "http://example.com:9000",
            "http://user:pass@127.0.0.1:9000",
            "http://127.0.0.1:9000/base",
            "http://127.0.0.1:9000?x=1",
            "https://127.0.0.1:9000",
        ):
            value = configuration()
            value["authority_url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(value)

    def test_duplicate_endpoint_aliases_cannot_bypass_topology(self):
        value = configuration()
        value["projection_urls"] = ["http://localhost:9000"]
        with self.assertRaises(ValueError):
            self.load(value)
        value = configuration()
        value["redis_nodes"][0][1] = {"host": "localhost", "port": 9002}
        with self.assertRaises(ValueError):
            self.load(value)
        value = configuration()
        value["redis_nodes"][0][0]["port"] = 9000
        with self.assertRaises(ValueError):
            self.load(value)

    def test_credentials_namespace_grace_and_topology_are_strict(self):
        for field, value in (
            ("game_token", "x\n" * 24),
            ("game_token", "汉" * 48),
            ("game_token", "a" * 48),
            ("redis_password", "short"),
            ("namespace", ""),
            ("late_grace", True),
            ("redis_nodes", []),
        ):
            config = configuration()
            config[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.load(config)

    def test_duplicate_nonfinite_or_nonobject_json_is_rejected(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', "[]"):
            with self.assertRaises(ValueError):
                self.load(raw=raw)


class CLILifecycleTests(unittest.TestCase):
    def arguments(self, role="projection"):
        return SimpleNamespace(
            role=role,
            config="unused",
            root="unused",
            shard=0,
            host="127.0.0.1",
            port=9000,
        )

    def test_bind_failure_starts_no_projection_and_still_closes_store(self):
        calls = []
        store = SimpleNamespace(
            run=lambda stop: calls.append("run"), close=lambda: calls.append("close")
        )
        with (
            patch("gaming_leaderboard.cli.Projection", return_value=store),
            patch("gaming_leaderboard.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                serve_role(self.arguments(), configuration())
        self.assertEqual(calls, ["close"])

    def test_projection_stops_and_joins_on_serving_failure(self):
        entered, exited = threading.Event(), threading.Event()
        calls = []

        def run(stop):
            self.assertFalse(threading.current_thread().daemon)
            entered.set()
            stop.wait()
            exited.set()

        def serving(**kwargs):
            self.assertTrue(entered.wait(1))
            raise OSError("serve failed")

        store = SimpleNamespace(run=run, close=lambda: calls.append(exited.is_set()))
        instance = SimpleNamespace(
            serve_forever=serving, server_close=lambda: calls.append("http closed")
        )
        with (
            patch("gaming_leaderboard.cli.Projection", return_value=store),
            patch("gaming_leaderboard.cli.server", return_value=instance),
        ):
            with self.assertRaises(OSError):
                serve_role(self.arguments(), configuration())
        self.assertEqual(calls, ["http closed", True])

    def test_listener_close_failure_does_not_skip_thread_drain_or_store_close(self):
        exited = threading.Event()

        def run(stop):
            stop.wait()
            exited.set()

        store = SimpleNamespace(run=run, close=lambda: self.assertTrue(exited.is_set()))
        instance = SimpleNamespace(
            serve_forever=lambda **kwargs: None,
            server_close=lambda: (_ for _ in ()).throw(OSError("close failed")),
        )
        with (
            patch("gaming_leaderboard.cli.Projection", return_value=store),
            patch("gaming_leaderboard.cli.server", return_value=instance),
        ):
            with self.assertRaises(OSError):
                serve_role(self.arguments(), configuration())
        self.assertTrue(exited.is_set())

    def test_authority_owned_pool_closes_when_binding_fails(self):
        closed = []
        with (
            patch("gaming_leaderboard.cli.Journal"),
            patch(
                "gaming_leaderboard.cli.Service",
                return_value=SimpleNamespace(close=lambda: closed.append(True)),
            ),
            patch("gaming_leaderboard.cli.server", side_effect=OSError("bind failed")),
        ):
            with self.assertRaises(OSError):
                serve_role(self.arguments("authority"), configuration())
        self.assertEqual(closed, [True])
