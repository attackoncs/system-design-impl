"""Transaction, lifecycle, privacy and routing regressions for the SQL store."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nearby_friends.core import Error, Ring, Store, meters, redis_url


class StoreReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.now = [2000000000.0]
        self.store = Store(self.path, clock=lambda: self.now[0])
        for name in ("alice", "bob"):
            self.store.provision(name, name + "-private", admin=name == "alice")
            self.store.setting(name, "sharing", True)

    def tearDown(self):
        self.temp.cleanup()

    def report(self, user="alice", **changes):
        return self.store.update(
            user, {"latitude": 0, "longitude": 0, "timestamp": self.now[0], **changes}
        )

    def befriend(self):
        self.store.friendship("alice", "bob", "friend.request")
        self.store.friendship("bob", "alice", "friend.accept")

    def assert_domain(self, code, function, *args):
        with self.assertRaises(Error) as failure:
            function(*args)
        self.assertEqual(failure.exception.code, code)

    def test_configuration_rejects_nonfinite_boolean_and_unrepresentable_values(self):
        for value in (
            True,
            False,
            0,
            -1,
            "600",
            None,
            float("nan"),
            float("inf"),
            10**1000,
            10**20,
            (2**63 - 1) / 1000,  # Float rounding must not allow Redis PX overflow.
        ):
            for key in ("ttl", "retention"):
                with (
                    self.subTest(key=key, value=type(value).__name__),
                    self.assertRaises(ValueError),
                ):
                    Store(self.path, **{key: value})
        for value in (None, "", ":memory:"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                Store(value)
        with self.assertRaises(ValueError):
            Store(self.path, clock=None)
        for value in (True, -1, float("nan"), float("inf"), 10**1000):
            with (
                self.subTest(clock=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                Store(self.path, clock=lambda value=value: value)

    def test_persisted_durations_are_authoritative_and_revalidated(self):
        restarted = Store(self.path, ttl=1, retention=2)
        self.assertEqual((restarted.ttl, restarted.retention), (600, 86400))
        with self.store.transaction(True) as db:
            db.execute("UPDATE config SET value='true' WHERE key='ttl'")
        with self.assertRaises(ValueError):
            Store(self.path)
        # Failed initialization must close the connection, including on Windows.
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE config SET value='600' WHERE key='ttl'")
            db.commit()
        self.assertEqual(Store(self.path).ttl, 600)

    def test_concurrent_initializers_choose_one_complete_configuration(self):
        path = Path(self.temp.name) / "new.sqlite"
        with ThreadPoolExecutor(8) as pool:
            results = list(
                pool.map(lambda i: Store(path, ttl=10 + i, retention=100 + i), range(8))
            )
        configs = {(store.ttl, store.retention) for store in results}
        self.assertEqual(len(configs), 1)
        ttl, retention = configs.pop()
        self.assertEqual(retention - ttl, 90)

    @contextmanager
    def queue_report(self, body):
        """Hold the real writer until the worker has entered BEGIN IMMEDIATE."""
        waiting = threading.Event()
        connect = self.store.connect

        def traced_connect():
            db = connect()
            db.set_trace_callback(
                lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
            return db

        with ThreadPoolExecutor(1) as pool:
            with self.store.transaction(True):
                with patch.object(self.store, "connect", side_effect=traced_connect):
                    future = pool.submit(self.store.update, "alice", body)
                    self.assertTrue(waiting.wait(2))
                    yield future
            # Exiting the writer lets the worker sample time and finish.

    def test_expired_report_waiting_for_writer_is_rejected_without_mutation(self):
        body = {"latitude": 0, "longitude": 0, "timestamp": self.now[0]}
        with self.queue_report(body) as future:
            self.now[0] += 600
        self.assert_domain(400, future.result)
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT last_ts FROM users WHERE id='alice'").fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM current").fetchone()[0], 0
            )

    def test_receipt_timestamp_and_ttl_start_after_writer_acquisition(self):
        stamp = self.now[0]
        with self.queue_report(
            {"latitude": 0, "longitude": 0, "timestamp": stamp}
        ) as future:
            self.now[0] += 5
        accepted = future.result()
        self.assertEqual(accepted["received_at"], stamp + 5)
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT expires FROM current").fetchone()[0], stamp + 605
            )

    def test_report_shapes_and_huge_numbers_have_domain_errors(self):
        for body in (
            None,
            [],
            {},
            {"latitude": 0, "longitude": 0, "timestamp": self.now[0], "unknown": 1},
        ):
            with self.subTest(body=body):
                self.assert_domain(400, self.store.update, "alice", body)
        for field in ("latitude", "longitude", "timestamp"):
            for value in (True, 10**1000, float("inf"), "0"):
                body = {
                    "latitude": 0,
                    "longitude": 0,
                    "timestamp": self.now[0],
                    field: value,
                }
                with self.subTest(field=field, value=type(value).__name__):
                    self.assert_domain(400, self.store.update, "alice", body)
        self.assert_domain(
            400,
            self.store.update,
            [],
            {"latitude": 0, "longitude": 0, "timestamp": self.now[0]},
        )

    def test_all_report_cutoffs_use_one_consistent_clock_sample(self):
        self.befriend()
        self.report()
        self.report("bob")
        values = iter([self.now[0] + 599, self.now[0] + 600])
        calls = []

        def clock():
            calls.append(1)
            return next(values)

        self.store.clock = clock
        own, friends = self.store.reports("alice")
        self.assertEqual(own["id"], "alice")
        self.assertEqual([friend["id"] for friend in friends], ["bob"])
        self.assertEqual(len(calls), 1)

    def test_data_isolation_and_consent_removal_survive_restart(self):
        self.befriend()
        self.store.setting("alice", "history", True)
        report = self.report()
        self.report("bob")
        report["latitude"] = 89
        own, friends = self.store.reports("alice")
        own["longitude"] = 179
        friends[0]["longitude"] = 100
        self.assertEqual(self.store.reports("alice")[0]["latitude"], 0)
        self.assertEqual(self.store.reports("alice")[1][0]["longitude"], 0)
        self.store.setting("alice", "history", False)
        restarted = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(restarted.read_history("alice"), [])
        with restarted.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM history").fetchone()[0], 0
            )

    def test_history_huge_offset_and_wrong_actor_are_domain_errors(self):
        for offset in (True, -1, 2**63, 10**1000):
            with self.subTest(offset=type(offset).__name__):
                self.assert_domain(400, self.store.read_history, "alice", offset)
        self.assert_domain(404, self.store.read_history, "missing")
        self.assert_domain(400, self.store.friends, [])
        for value in (None, [], 123, "x" * 4097):
            with self.subTest(token=type(value).__name__):
                self.assert_domain(401, self.store.authenticate, value)

    def test_index_upgrade_preserves_data_and_avoids_unindexed_expiry_scan(self):
        self.report()
        with self.store.transaction(True) as db:
            db.execute("DROP INDEX current_expiry")
            db.execute("DROP INDEX friends_recipient")
        upgraded = Store(self.path, clock=lambda: self.now[0])
        self.assertEqual(upgraded.reports("alice")[0]["id"], "alice")
        with upgraded.transaction() as db:
            expiry = " ".join(
                str(row[3])
                for row in db.execute(
                    "EXPLAIN QUERY PLAN DELETE FROM current WHERE expires<=?",
                    (self.now[0],),
                )
            )
            friendship = " ".join(
                str(row[3])
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT a,b FROM friends WHERE a=? OR b=?",
                    ("alice", "alice"),
                )
            )
        self.assertIn("current_expiry", expiry)
        self.assertIn("friends_recipient", friendship)

    def test_update_does_not_sweep_unrelated_users_on_each_hot_write(self):
        self.report("bob")
        self.now[0] += 601
        self.report()
        self.assertIsNone(self.store.reports("bob")[0])
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM current").fetchone()[0], 2
            )
        self.store.sweep()
        with self.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM current").fetchone()[0], 1
            )

    def test_concurrent_devices_preserve_strict_user_order_and_unique_revisions(self):
        self.store.setting("alice", "history", True)

        def update(index):
            try:
                return self.report(
                    timestamp=self.now[0] + index, device=f"device{index}"
                )
            except Error as error:
                self.assertEqual(error.code, 409)
                return None

        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(update, range(1, 21)))
        accepted = [report for report in results if report is not None]
        revisions = [report["revision"] for report in accepted]
        self.assertEqual(len(revisions), len(set(revisions)))
        current, _ = self.store.reports("alice")
        self.assertEqual(current["timestamp"], self.now[0] + 20)
        history = self.store.read_history("alice")
        self.assertEqual(len(history), len(accepted))
        timestamps = [report["timestamp"] for report in history]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_history_failure_rolls_back_presence_revision_and_timestamp(self):
        original = self.report()
        self.store.setting("alice", "history", True)
        with self.store.transaction(True) as db:
            db.execute("""CREATE TRIGGER fail_history BEFORE INSERT ON history BEGIN
                SELECT RAISE(ABORT,'injected history failure'); END""")
        self.now[0] += 1
        with self.assertRaises(sqlite3.IntegrityError):
            self.report()
        with self.store.transaction() as db:
            user = db.execute(
                "SELECT revision,last_ts FROM users WHERE id='alice'"
            ).fetchone()
            body = json.loads(
                db.execute("SELECT body FROM current WHERE user='alice'").fetchone()[0]
            )
            count = db.execute("SELECT count(*) FROM history").fetchone()[0]
        self.assertEqual(tuple(user), (original["revision"], original["timestamp"]))
        self.assertEqual(body, original)
        self.assertEqual(count, 0)

    def test_queued_report_cannot_restore_location_after_privacy_revocation(self):
        self.report()
        self.now[0] += 1
        waiting = threading.Event()
        connect = self.store.connect

        def traced_connect():
            db = connect()
            db.set_trace_callback(
                lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
            return db

        with ThreadPoolExecutor(1) as pool:
            with self.store.transaction(True) as db:
                with patch.object(self.store, "connect", side_effect=traced_connect):
                    future = pool.submit(self.report)
                    self.assertTrue(waiting.wait(2))
                    db.execute(
                        "UPDATE users SET sharing=0,revision=revision+1 WHERE id='alice'"
                    )
                    db.execute("DELETE FROM current WHERE user='alice'")
            self.assert_domain(403, future.result)
        self.assertIsNone(self.store.reports("alice")[0])

    def test_ring_install_owns_validated_nodes_while_waiting_for_writer(self):
        nodes = {"a": "redis://localhost:6379"}
        waiting = threading.Event()
        connect = self.store.connect

        def traced_connect():
            db = connect()
            db.set_trace_callback(
                lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
            )
            return db

        with ThreadPoolExecutor(1) as pool:
            with self.store.transaction(True):
                with patch.object(self.store, "connect", side_effect=traced_connect):
                    future = pool.submit(self.store.set_ring, "alice", nodes, 0)
                    self.assertTrue(waiting.wait(2))
                    nodes["a"] = "http://unvalidated.example"
            self.assertEqual(future.result(), {"epoch": 1})
        self.assertEqual(self.store.ring()["nodes"], {"a": "redis://localhost:6379"})


class RingReliabilityTests(unittest.TestCase):
    def test_url_validation_rejects_timeout_overrides_and_invalid_endpoints(self):
        invalid = [
            None,
            [],
            123,
            "",
            "http://localhost:6379",
            "redis://localhost:0",
            "redis://localhost:65536",
            "redis://localhost:abc",
            "redis://localhost/foo",
            "redis://localhost/1#fragment",
            "redis://localhost/0?socket_timeout=999",
            "redis://localhost/0?db=1&db=2",
            "redis://localhost/0?db=",
            "redis://localhost/0?db=2147483648",
            "redis://local\nhost",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(Error) as failure:
                redis_url(url)
            self.assertEqual(failure.exception.code, 400)
        for url in (
            "redis://localhost",
            "rediss://alice:p%40ss@[::1]:6379/0",
            "redis://localhost/1?db=2",
        ):
            self.assertEqual(redis_url(url), url)

    def test_ring_owns_immutable_nodes_and_tokens(self):
        nodes = {"a": "redis://localhost:6379", "b": "redis://localhost:6380"}
        ring = Ring(nodes)
        owners = [ring.owner(str(i)) for i in range(100)]
        nodes["a"] = "redis://elsewhere:6380"
        nodes["c"] = "redis://localhost:6381"
        self.assertEqual(ring.nodes["a"], "redis://localhost:6379")
        self.assertEqual(owners, [ring.owner(str(i)) for i in range(100)])
        with self.assertRaises(TypeError):
            ring.nodes["a"] = "redis://elsewhere:6379"
        self.assertIsInstance(ring.tokens, tuple)
        self.assertIsInstance(ring.points, tuple)
        for user in (None, [], "bad:id"):
            with self.subTest(user=user), self.assertRaises(Error):
                ring.owner(user)

    def test_distance_stability_and_validation(self):
        near_antipode = {"latitude": 0.00000001, "longitude": 180}
        origin = {"latitude": 0, "longitude": 0}
        expected = 6371008.8 * (math.pi - math.radians(0.00000001))
        self.assertAlmostEqual(meters(origin, near_antipode), expected, places=6)
        for body in (
            None,
            {},
            {"latitude": True, "longitude": 0},
            {"latitude": 91, "longitude": 0},
            {"latitude": 0, "longitude": 10**1000},
        ):
            with self.subTest(body=body), self.assertRaises(Error):
                meters(origin, body)


if __name__ == "__main__":
    unittest.main()
