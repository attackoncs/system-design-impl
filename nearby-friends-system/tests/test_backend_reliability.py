"""Backend fault-injection and resource tests; these do not emulate Redis integration."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import redis

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nearby_friends.backend import Backend
from nearby_friends.core import Error, Store, dumps


class RepairBatch:
    def __init__(self, client):
        self.client, self.calls = client, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def eval(self, *args):
        self.calls.append(args)

    def execute(self):
        self.client.batches.append(self.calls)
        if self.client.batch_hook:
            self.client.batch_hook()
        return [1] * len(self.calls)


class InstrumentedRedis:
    """Record client calls and inject delays/failures without claiming wire fidelity."""

    def __init__(self):
        self.values = None
        self.mget_hook = self.publish_hook = self.batch_hook = self.eval_hook = None
        self.batches, self.published, self.warms = [], [], []
        self.mget_calls, self.closed = 0, 0

    def mget(self, keys):
        self.mget_calls += 1
        if self.mget_hook:
            self.mget_hook()
        return [None] * len(keys) if self.values is None else self.values

    def pipeline(self, transaction):
        if transaction:
            raise AssertionError("repairs must not create one large Redis transaction")
        return RepairBatch(self)

    def eval(self, *args):
        self.warms.append(args)
        if self.eval_hook:
            self.eval_hook()
        return 1

    def publish(self, channel, value):
        self.published.append((channel, value))
        if self.publish_hook:
            self.publish_hook()
        return 1

    def close(self):
        self.closed += 1


class BackendReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [2000000000.0]
        self.store = Store(
            Path(self.temp.name) / "state.sqlite", clock=lambda: self.now[0]
        )
        for user in ("alice", "bob"):
            self.store.provision(user, user + "-private", admin=user == "alice")
            self.store.setting(user, "sharing", True)
            self.store.update(user, self.location())
        self.store.friendship("alice", "bob", "friend.request")
        self.store.friendship("bob", "alice", "friend.accept")
        self.clients, self.created = {}, []

        def factory(url, **kwargs):
            self.created.append((url, kwargs))
            return self.clients.setdefault(url, InstrumentedRedis())

        self.factory = patch(
            "nearby_friends.backend.redis.Redis.from_url", side_effect=factory
        )
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.backend = Backend(self.store, "redis://cache")
        self.addCleanup(self.backend.close)
        self.cache = self.clients["redis://cache"]

    def location(self, latitude=0, longitude=0):
        return {"latitude": latitude, "longitude": longitude, "timestamp": self.now[0]}

    def seed_friends(self, count):
        """Fixture writes only: avoid timing hundreds of unrelated account APIs."""
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM friends")
            for index in range(count):
                user = f"u{index:04d}"
                report = {
                    "id": user,
                    "latitude": 0,
                    "longitude": 0,
                    "timestamp": self.now[0],
                    "received_at": self.now[0],
                    "revision": 1,
                    "device": "default",
                }
                db.execute("INSERT INTO users(id,sharing) VALUES(?,1)", (user,))
                db.execute("INSERT INTO friends VALUES('alice',?)", (user,))
                db.execute(
                    "INSERT INTO current VALUES(?,?,?)",
                    (user, dumps(report), self.now[0] + self.store.ttl),
                )

    def set_broker(self, number):
        url = f"redis://broker-{number}"
        self.store.set_ring("alice", {"owner": url}, self.store.ring()["epoch"])
        return url

    def test_corrupt_and_short_cache_results_do_not_drop_or_replace_sql_rows(self):
        self.seed_friends(3)
        self.cache.values = ['{"id":"u0000","longitude":90}']
        rows = self.backend.snapshot("alice")
        self.assertEqual([row["id"] for row in rows], ["u0000", "u0001", "u0002"])
        self.assertTrue(all(row["distance_m"] == 0 for row in rows))
        self.assertTrue(
            all("device" not in row and "received_at" not in row for row in rows)
        )
        rows[0]["latitude"] = 90
        self.assertEqual(self.backend.snapshot("alice")[0]["latitude"], 0)
        self.assertEqual(len(self.cache.batches[0]), 3)

    def test_cache_outage_does_not_add_one_repair_timeout_per_friend(self):
        self.seed_friends(300)

        def fail():
            raise redis.ConnectionError("injected cache failure")

        self.cache.mget_hook = fail
        self.assertEqual(len(self.backend.snapshot("alice")), 300)
        self.assertEqual(self.cache.mget_calls, 1)
        self.assertEqual(self.cache.batches, [])
        self.assertEqual(self.cache.warms, [])

    def test_invalid_utf8_reply_falls_back_without_exposing_a_decode_failure(self):
        def corrupt():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "injected corruption")

        self.cache.mget_hook = corrupt
        self.assertEqual([row["id"] for row in self.backend.snapshot("alice")], ["bob"])
        self.assertEqual(self.cache.batches, [])

    def test_missing_records_are_repaired_in_bounded_network_batches(self):
        self.seed_friends(300)
        self.assertEqual(len(self.backend.snapshot("alice")), 300)
        self.assertEqual([len(batch) for batch in self.cache.batches], [128, 128, 44])
        self.assertEqual(self.cache.warms, [])
        self.cache.values = [dumps(row) for row in self.store.reports("alice")[1]]
        self.cache.batches.clear()
        self.assertEqual(len(self.backend.snapshot("alice")), 300)
        self.assertEqual(self.cache.batches, [])

    def test_repair_budget_stops_between_batches_and_keeps_complete_sql_result(self):
        self.seed_friends(300)
        clock = [0]
        self.cache.batch_hook = lambda: clock.__setitem__(0, 2)
        with patch(
            "nearby_friends.backend.time.monotonic", side_effect=lambda: clock[0]
        ):
            self.assertEqual(len(self.backend.snapshot("alice")), 300)
        self.assertEqual([len(batch) for batch in self.cache.batches], [128])

    def test_failed_repair_stops_later_batches(self):
        self.seed_friends(300)

        def fail():
            raise redis.TimeoutError("injected repair failure")

        self.cache.batch_hook = fail
        self.assertEqual(len(self.backend.snapshot("alice")), 300)
        self.assertEqual([len(batch) for batch in self.cache.batches], [128])

    def test_cache_wait_cannot_return_a_friend_revoked_during_network_io(self):
        self.cache.mget_hook = lambda: self.store.friendship(
            "alice", "bob", "friend.remove"
        )
        self.assertEqual(self.backend.snapshot("alice"), [])

    def test_cache_wait_cannot_extend_either_users_presence(self):
        self.cache.mget_hook = lambda: self.now.__setitem__(
            0, self.now[0] + self.store.ttl
        )
        self.assertEqual(self.backend.snapshot("alice"), [])
        self.assertEqual(self.cache.batches[0], [])

    def test_cache_wait_rechecks_viewer_movement(self):
        def move():
            self.now[0] += 1
            self.store.update("alice", self.location(latitude=1))

        self.cache.mget_hook = move
        self.assertEqual(self.backend.snapshot("alice"), [])

    def test_update_durable_success_survives_cache_and_hint_failures(self):
        url = self.set_broker(1)
        broker = self.clients.setdefault(url, InstrumentedRedis())

        def fail():
            raise redis.ConnectionError("injected outage")

        self.cache.eval_hook = broker.publish_hook = fail
        self.now[0] += 1
        result = self.backend.update("bob", self.location(longitude=0.001))
        self.assertFalse(result["hint_published"])
        report = self.store.reports("alice")[1][0]
        self.assertEqual(report["revision"], result["revision"])
        self.assertEqual(report["longitude"], 0.001)
        self.assertEqual(json.loads(broker.published[0][1]), {"user": "bob"})

    def test_warm_preserves_receipt_based_lifetime_and_skips_expired_records(self):
        report = self.store.reports("alice")[1][0]
        self.now[0] += 10
        self.assertTrue(self.backend.warm(report))
        args = self.cache.warms[-1]
        self.assertEqual(args[-1], int((self.store.ttl - 10) * 1000))
        self.now[0] += self.store.ttl
        self.assertFalse(self.backend.warm(report))
        self.assertEqual(len(self.cache.warms), 1)

    def test_broker_reconfiguration_reclaims_idle_clients_and_bounds_pool(self):
        self.backend.close()
        self.backend = Backend(self.store, "redis://cache", broker_capacity=2)
        self.addCleanup(self.backend.close)
        for number in range(6):
            url = self.set_broker(number)
            self.assertTrue(self.backend.publish("alice"))
            self.assertLessEqual(len(self.backend.brokers), 2)
        self.assertEqual(
            set(self.backend.brokers), {"redis://broker-4", "redis://broker-5"}
        )
        for number in range(4):
            self.assertEqual(self.clients[f"redis://broker-{number}"].closed, 1)
        self.assertEqual(self.clients[url].closed, 0)

    def test_concurrent_publish_creates_one_client_and_never_closes_an_active_lease(
        self,
    ):
        url = self.set_broker(1)
        broker = self.clients.setdefault(url, InstrumentedRedis())
        entered, release = threading.Event(), threading.Event()

        def wait():
            entered.set()
            self.assertTrue(release.wait(3))
            self.assertEqual(broker.closed, 0)

        broker.publish_hook = wait
        with ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(self.backend.publish, "alice") for _ in range(8)]
            try:
                self.assertTrue(entered.wait(1))
            finally:
                release.set()
            self.assertTrue(all(future.result(2) for future in futures))
        self.assertEqual(sum(created_url == url for created_url, _ in self.created), 1)
        self.assertEqual(broker.closed, 0)

    def test_full_pool_of_active_clients_drops_hint_without_growing_or_evicting(self):
        self.backend.close()
        self.backend = Backend(self.store, "redis://cache", broker_capacity=1)
        self.addCleanup(self.backend.close)
        first = self.set_broker(1)
        broker = self.clients.setdefault(first, InstrumentedRedis())
        entered, release = threading.Event(), threading.Event()
        broker.publish_hook = lambda: (entered.set(), release.wait(3))
        with ThreadPoolExecutor(2) as pool:
            publishing = pool.submit(self.backend.publish, "alice")
            try:
                self.assertTrue(entered.wait(1))
                self.set_broker(2)
                self.assertFalse(self.backend.publish("alice"))
                self.assertNotIn("redis://broker-2", self.clients)
                self.assertEqual(broker.closed, 0)
            finally:
                release.set()
            self.assertTrue(publishing.result(2))
        self.assertTrue(self.backend.publish("alice"))
        self.assertEqual(broker.closed, 1)

    def test_close_drains_accepted_work_then_rejects_new_work(self):
        entered, release = threading.Event(), threading.Event()
        self.cache.mget_hook = lambda: (entered.set(), release.wait(3))
        with ThreadPoolExecutor(3) as pool:
            snapshot = pool.submit(self.backend.snapshot, "alice")
            try:
                self.assertTrue(entered.wait(1))
                closing = pool.submit(self.backend.close)
                with self.backend._condition:
                    self.assertTrue(
                        self.backend._condition.wait_for(
                            lambda: self.backend._closing, timeout=1
                        )
                    )
                self.assertFalse(closing.done())
                self.assertEqual(self.cache.closed, 0)
                with self.assertRaises(Error) as caught:
                    self.backend.publish("alice")
                self.assertEqual(caught.exception.code, 503)
            finally:
                release.set()
            self.assertEqual(len(snapshot.result(2)), 1)
            closing.result(2)
        self.assertEqual(self.cache.closed, 1)
        self.backend.close()
        self.assertEqual(self.cache.closed, 1)

    def test_client_configuration_bounds_connections_and_disables_hidden_retries(self):
        self.set_broker(1)
        self.backend.publish("alice")
        for _, settings in self.created:
            self.assertEqual(settings["max_connections"], 16)
            self.assertEqual(settings["socket_timeout"], 0.5)
            self.assertEqual(settings["socket_connect_timeout"], 0.5)
            self.assertEqual(settings["retry"].get_retries(), 0)

    def test_constructor_and_radius_validation(self):
        for namespace in (None, True, "", "a:b", "x" * 65):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                Backend(self.store, "redis://cache", namespace)
        for capacity in (True, 0, 33, 1.0):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                Backend(self.store, "redis://cache", broker_capacity=capacity)
        for budget in (True, 0, -1, float("inf"), float("nan"), 10**1000):
            with (
                self.subTest(budget=type(budget).__name__),
                self.assertRaises(ValueError),
            ):
                Backend(self.store, "redis://cache", repair_budget=budget)
        with self.assertRaises(Error):
            Backend(self.store, "redis://cache?socket_timeout=100")
        for radius in (True, 0, -1, float("nan"), float("inf"), 10**1000, "5"):
            with self.subTest(radius=type(radius).__name__), self.assertRaises(Error):
                self.backend.snapshot("alice", radius)


if __name__ == "__main__":
    unittest.main()
