"""Actual SQLite state with explicit provider/clock fault injection."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from metrics_monitor.alerts import Alerts, Provider, WebhookProvider
from metrics_monitor.common import Error
from metrics_monitor.storage import Store


class RecordingProvider(Provider):
    def __init__(self, failure=False):
        self.failure = failure
        self.calls = []

    def deliver(self, destination, channel, payload, key):
        self.calls.append((key, json.loads(json.dumps(payload))))
        if self.failure:
            raise OSError("controlled lost confirmation")


class AlertReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [2_000_001_600.0]
        self.store = Store(
            Path(self.temp.name) / "metrics.sqlite", clock=lambda: self.now[0]
        )
        self.alerts = Alerts(self.store)

    def rule(self, **changes):
        return {
            "id": "hot",
            "metric": "cpu",
            "op": ">",
            "threshold": 80,
            "destination": "test",
            **changes,
        }

    def ingest(self, value=90, instance="host", stamp=None):
        stamp = self.now[0] if stamp is None else stamp
        self.store.ingest(
            f"b-{instance}-{stamp}-{value}",
            [
                {
                    "metric": "cpu",
                    "labels": {"instance": instance},
                    "timestamp": stamp,
                    "value": value,
                }
            ],
        )

    def fire(self):
        self.alerts.put_rule(self.rule())
        self.ingest()
        self.alerts.evaluate()

    def test_atomic_rule_import_validates_every_rule_before_writes(self):
        with self.assertRaises(ValueError):
            self.alerts.put_rules(
                [self.rule(), self.rule(id="bad", group_by=[["invalid"]])]
            )
        self.assertEqual(self.alerts.status()["rules"], [])

    def test_static_severity_labels_are_owned_and_do_not_change_metric_selection(self):
        rule = self.rule(labels={"severity": "page"}, group_by=["severity"])
        self.alerts.put_rule(rule)
        rule["labels"]["severity"] = "changed"
        self.ingest()
        result = self.alerts.evaluate()
        self.assertEqual(json.loads(result["alerts"][0]["labels"])["severity"], "page")
        provider = RecordingProvider()
        self.assertTrue(self.alerts.notify_once(provider))
        self.assertEqual(
            provider.calls[0][1]["events"][0]["labels"]["severity"], "page"
        )

    def test_rule_conflict_rolls_back_other_new_rules(self):
        self.alerts.put_rule(self.rule())
        with self.assertRaises(Error):
            self.alerts.put_rules([self.rule(id="new"), self.rule(threshold=70)])
        self.assertEqual(
            [rule["id"] for rule in self.alerts.status()["rules"]], ["hot"]
        )

    def test_rule_file_import_replay_and_duplicate_fields(self):
        path = Path(self.temp.name) / "rules.json"
        path.write_text(json.dumps({"rules": [self.rule()]}), encoding="utf-8")
        first = self.alerts.load_rules(path)
        self.assertEqual(self.alerts.load_rules(path), first)
        path.write_text('[{"id":"one","id":"two"}]', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.alerts.load_rules(path)
        self.assertEqual(self.alerts.status()["counts"]["rules"], 1)

    def test_rule_file_size_limit_precedes_json_decode(self):
        path = Path(self.temp.name) / "large.json"
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            self.alerts.load_rules(path)

    def test_rule_inputs_and_returned_containers_are_isolated(self):
        body = self.rule(group_by=["instance"], match={"env": "prod"})
        returned = self.alerts.put_rule(body)
        body["group_by"].append("other")
        returned["match"]["env"] = "mutated"
        with self.store.transaction() as db:
            stored = json.loads(db.execute("SELECT body FROM rules").fetchone()[0])
        self.assertEqual(stored["group_by"], ["instance"])
        self.assertEqual(stored["match"], {"env": "prod"})

    def test_rule_and_silence_identifiers_are_idempotent(self):
        configured = self.alerts.put_rule(self.rule())
        self.assertEqual(self.alerts.put_rule(self.rule()), configured)
        first = self.alerts.silence(
            {"instance": "host"}, self.now[0], self.now[0] + 10, id="quiet"
        )
        self.assertEqual(
            self.alerts.silence(
                {"instance": "host"}, self.now[0], self.now[0] + 10, id="quiet"
            ),
            first,
        )
        with self.assertRaises(Error):
            self.alerts.silence({}, self.now[0], self.now[0] + 10, id="quiet")

    def test_evaluation_aggregation_does_not_hold_sql_writer(self):
        self.alerts.put_rule(self.rule())
        self.ingest()
        entered, release, written = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        original = self.alerts._evaluate_snapshot

        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        with (
            patch.object(self.alerts, "_evaluate_snapshot", side_effect=blocked),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            evaluation = executor.submit(self.alerts.evaluate)
            try:
                self.assertTrue(entered.wait(1))
                write = executor.submit(
                    lambda: (self.ingest(1, instance="other"), written.set())
                )
                self.assertTrue(written.wait(1))
                write.result()
            finally:
                release.set()
            evaluation.result()

    def test_concurrent_evaluators_publish_one_transition(self):
        self.alerts.put_rule(self.rule())
        self.ingest()
        other = Alerts(Store(self.store.path, clock=lambda: self.now[0]))
        barrier = threading.Barrier(2)
        first_calls = set()
        lock = threading.Lock()

        def synchronized(instance):
            original = instance._evaluate_snapshot

            def compute(*args):
                with lock:
                    first = id(instance) not in first_calls
                    first_calls.add(id(instance))
                if first:
                    barrier.wait(2)
                return original(*args)

            return compute

        with (
            patch.object(
                self.alerts, "_evaluate_snapshot", side_effect=synchronized(self.alerts)
            ),
            patch.object(other, "_evaluate_snapshot", side_effect=synchronized(other)),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = [
                executor.submit(instance.evaluate) for instance in (self.alerts, other)
            ]
            for result in results:
                result.result(3)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.alerts.status()["alerts"][0]["transition"], 1)

    def test_stale_computation_cannot_revert_a_newer_resolution(self):
        self.fire()
        other = Alerts(Store(self.store.path, clock=lambda: self.now[0]))
        entered, release = threading.Event(), threading.Event()
        original = self.alerts._evaluate_snapshot
        first = [True]

        def blocked(*args):
            if first[0]:
                first[0] = False
                entered.set()
                release.wait(3)
            return original(*args)

        with (
            patch.object(self.alerts, "_evaluate_snapshot", side_effect=blocked),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            old = executor.submit(self.alerts.evaluate)
            try:
                self.assertTrue(entered.wait(1))
                self.now[0] += 1
                self.ingest(1)
                other.evaluate()
            finally:
                release.set()
            old.result(3)
        state = self.alerts.status()["alerts"][0]
        # The retried false evaluation advances resolved to inactive, but must
        # neither restore firing nor create another transition.
        self.assertEqual(
            (state["state"], state["transition"], state["value"]), ("inactive", 2, 1)
        )

    def test_snapshot_does_not_mix_new_raw_data_with_old_selection(self):
        self.alerts.put_rule(self.rule())
        self.ingest(1, stamp=self.now[0] - 1)
        original = self.store.select
        injected = [False]

        def selected(*args):
            rows = original(*args)
            if not injected[0]:
                injected[0] = True
                self.ingest(90)
            return rows

        with patch.object(self.store, "select", side_effect=selected):
            self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"], "inactive")
        self.assertEqual(self.alerts.evaluate()["alerts"][0]["state"], "firing")

    def test_sample_budget_is_total_per_rule_and_failure_is_visible(self):
        self.alerts.put_rule(self.rule(window=3600))
        self.ingest(90, instance="a")
        self.ingest(90, instance="b")
        with self.store.transaction(True) as db:
            series = [row[0] for row in db.execute("SELECT id FROM series")]
            db.executemany(
                "INSERT INTO raw VALUES(?,?,?)",
                [
                    (sid, self.now[0] - 2000 + index * 0.1, 90)
                    for sid in series
                    for index in range(10001)
                ],
            )
        status = self.alerts.evaluate()
        self.assertEqual(status["alerts"], [])
        self.assertIn("budget", status["rules"][0]["error"])

    def test_status_reports_explicit_truncation(self):
        self.alerts.put_rules([self.rule(id="first"), self.rule(id="second")])
        status = self.alerts.status(limit=1)
        self.assertEqual(len(status["rules"]), 1)
        self.assertEqual(status["counts"]["rules"], 2)
        self.assertTrue(status["truncated"]["rules"])

    def test_pending_interval_resets_after_unobserved_gap(self):
        self.alerts.put_rule(self.rule(for_seconds=60, max_age=10))
        self.ingest()
        self.alerts.evaluate()
        self.now[0] += 61
        self.ingest()
        state = self.alerts.evaluate()["alerts"][0]
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["since"], self.now[0])

    def test_clock_rollback_cannot_regress_evaluated_state(self):
        self.fire()
        accepted = self.alerts.status()["alerts"][0]
        self.now[0] -= 100
        self.assertEqual(self.alerts.evaluate()["alerts"][0], accepted)

    def test_two_notification_workers_do_not_share_live_lease(self):
        self.fire()
        other = Alerts(Store(self.store.path, clock=lambda: self.now[0]))
        entered, release = threading.Event(), threading.Event()
        provider = RecordingProvider()
        original = provider.deliver

        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        with (
            patch.object(provider, "deliver", side_effect=blocked),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            first = executor.submit(self.alerts.notify_once, provider)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(other.notify_once(RecordingProvider()))
            finally:
                release.set()
            self.assertTrue(first.result(3))
        self.assertEqual(self.alerts.status()["deliveries"][0]["attempts"], 1)

    def test_expired_lease_result_cannot_overwrite_new_owner_success(self):
        self.fire()
        other = Alerts(Store(self.store.path, clock=lambda: self.now[0]))
        entered, release = threading.Event(), threading.Event()
        first_provider = RecordingProvider(failure=True)
        original = first_provider.deliver

        def blocked(*args):
            entered.set()
            release.wait(3)
            return original(*args)

        second_provider = RecordingProvider()
        with (
            patch.object(first_provider, "deliver", side_effect=blocked),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            first = executor.submit(self.alerts.notify_once, first_provider)
            try:
                self.assertTrue(entered.wait(1))
                self.now[0] += 31
                self.assertTrue(other.notify_once(second_provider))
            finally:
                release.set()
            first.result(3)
        delivery = self.alerts.status()["deliveries"][0]
        self.assertEqual(
            (delivery["status"], delivery["attempts"], delivery["error"]),
            ("sent", 2, ""),
        )
        self.assertEqual(first_provider.calls[0], second_provider.calls[0])

    def test_crashed_claims_consume_attempt_budget(self):
        self.fire()

        class Crash(Provider):
            def deliver(self, *args):
                raise SystemExit("controlled worker crash")

        for _ in range(5):
            with self.assertRaises(SystemExit):
                self.alerts.notify_once(Crash())
            self.now[0] += 31
        self.assertFalse(self.alerts.notify_once(RecordingProvider()))
        delivery = self.alerts.status()["deliveries"][0]
        self.assertEqual((delivery["status"], delivery["attempts"]), ("dead", 5))

    def test_retry_payload_is_frozen_even_if_provider_mutates_argument(self):
        self.fire()
        seen = []

        class Mutating(Provider):
            def deliver(self, destination, channel, payload, key):
                seen.append((key, json.loads(json.dumps(payload))))
                payload["events"].clear()
                raise OSError("controlled failure")

        self.assertTrue(self.alerts.notify_once(Mutating()))
        self.now[0] += 2
        provider = RecordingProvider()
        self.assertTrue(self.alerts.notify_once(provider))
        self.assertEqual(seen[0], provider.calls[0])
        self.assertEqual(self.alerts.status()["deliveries"][0]["error"], "")

    def test_group_fifo_keeps_resolution_behind_failed_firing(self):
        self.fire()
        first = RecordingProvider(failure=True)
        self.alerts.notify_once(first)
        self.now[0] += 1
        self.ingest(1)
        self.alerts.evaluate()
        succeeding = RecordingProvider()
        self.assertFalse(self.alerts.notify_once(succeeding))
        self.now[0] += 1
        self.assertTrue(self.alerts.notify_once(succeeding))
        self.assertTrue(self.alerts.notify_once(succeeding))
        self.assertEqual(
            [body["events"][0]["state"] for _, body in succeeding.calls],
            ["firing", "resolved"],
        )

    def test_frozen_silenced_delivery_retains_key_and_content(self):
        self.fire()
        first = RecordingProvider(failure=True)
        self.alerts.notify_once(first)
        self.alerts.silence({"instance": "host"}, self.now[0], self.now[0] + 10)
        self.now[0] += 2
        second = RecordingProvider()
        self.assertFalse(self.alerts.notify_once(second))
        self.now[0] += 8
        self.assertTrue(self.alerts.notify_once(second))
        self.assertEqual(first.calls[0], second.calls[0])

    def test_provider_configuration_is_owned_and_missing_channels_fail(self):
        config = {"test": {"url": "http://original"}}
        provider = WebhookProvider(config)
        config["test"]["url"] = "http://changed"
        provider.destinations["test"]["url"] = "http://also-changed"
        with patch(
            "metrics_monitor.alert_providers.http_json", return_value={}
        ) as request:
            provider.deliver("test", "webhook", {}, "key")
        self.assertEqual(request.call_args.args[0], "http://original")
        with self.assertRaises(RuntimeError):
            provider.deliver("test", "email", {}, "key")


if __name__ == "__main__":
    unittest.main()
