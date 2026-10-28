import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from helpers_review import configuration
from stock_exchange.cli import load_config, serve_role, poll_once
from stock_exchange.admission import OwnerRate
from stock_exchange.common import Error


class CLITests(unittest.TestCase):
    def config(self, value):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "config.json"
            p.write_text(
                value if isinstance(value, str) else json.dumps(value), encoding="utf-8"
            )
            return load_config(p)

    def test_config_canonicalizes_localhost_and_rejects_role_alias_collision(self):
        value = configuration()
        self.assertEqual(self.config(value), value)
        value["engines"]["backup"] = "http://localhost:9890"
        with self.assertRaises(ValueError):
            self.config(value)

    def test_config_rejects_ambiguous_json_urls_and_credentials(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                self.config(raw)
        for endpoint in (
            "http://remote.example:9890",
            "http://user:pw@127.0.0.1:9890",
            "http://127.0.0.1:9890#frag",
            "http://127.0.0.1:9890?q=1",
        ):
            value = configuration()
            value["gateway"] = endpoint
            with self.assertRaises(ValueError):
                self.config(value)
        value = configuration()
        value["internal_token"] = "\u5bc6" * 24
        with self.assertRaises(ValueError):
            self.config(value)

    def test_boolean_time_invalid_session_and_unknown_opening_position_fail(self):
        value = configuration()
        value["simulation_timestamp"] = True
        with self.assertRaises(ValueError):
            self.config(value)
        value = configuration()
        value["settings"]["session"]["weekdays"] = [True]
        with self.assertRaises(ValueError):
            self.config(value)
        value = configuration()
        value["settings"]["accounts"]["alice"]["positions"]["OTHER"] = 1
        with self.assertRaises(ValueError):
            self.config(value)

    def test_binding_failure_starts_no_poll_thread(self):
        args = SimpleNamespace(role="primary", root="unused")
        with (
            patch("stock_exchange.cli.Engine"),
            patch("stock_exchange.cli.server", side_effect=OSError("busy")),
            patch("stock_exchange.cli.threading.Thread") as thread,
        ):
            with self.assertRaises(OSError):
                serve_role(args, configuration())
            thread.assert_not_called()

    def test_listener_close_failure_still_stops_and_joins_polling(self):
        entered, observed = threading.Event(), []

        def poll(role, store, config, stop):
            entered.set()
            stop.wait(1)
            observed.append(stop.is_set())

        class Listener:
            def serve_forever(self, **kwargs):
                if not entered.wait(1):
                    raise AssertionError("poll never started")
                raise RuntimeError("stopped")

            def server_close(self):
                raise OSError("close failed")

        with (
            patch("stock_exchange.cli.Engine"),
            patch("stock_exchange.cli.server", return_value=Listener()),
            patch("stock_exchange.cli.poll_once", side_effect=poll),
        ):
            with self.assertRaises(OSError):
                serve_role(
                    SimpleNamespace(role="primary", root="unused"), configuration()
                )
        self.assertEqual(observed, [True])
        self.assertFalse(
            any(
                thread.name == "exchange-primary-poll"
                for thread in threading.enumerate()
            )
        )

    def test_submission_rate_is_per_owner_and_refills_without_adding_identities(self):
        clock = [0.0]
        limiter = OwnerRate(["alice", "bob"], clock=lambda: clock[0], rate=2, burst=2)
        limiter.take("alice")
        limiter.take("alice")
        with self.assertRaises(Error) as error:
            limiter.take("alice")
        self.assertEqual(error.exception.status, 429)
        limiter.take("bob")
        clock[0] = 0.5
        limiter.take("alice")
        self.assertEqual(len(limiter.values), 2)

    def test_poll_rejects_oversized_feed_before_applying_any_commands(self):
        with (
            patch("stock_exchange.cli.request", return_value={"events": [{}] * 101}),
            patch("stock_exchange.cli.Engine") as engine,
        ):
            engine.cursor.return_value = {"seq": 1}
            with self.assertRaises(ValueError):
                poll_once("primary", engine, configuration(), threading.Event())
            engine.apply.assert_not_called()
