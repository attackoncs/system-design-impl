"""Chapter 2 gaps: geo-failover, fenced promotion, and cache policies."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from helpers_review import configuration
from scale_from_zero.cache import Cache, CacheCluster
from scale_from_zero.common import Error, request
from scale_from_zero.http import server
from scale_from_zero.leadership import Fence
from scale_from_zero.regions import GeoDNS
from scale_from_zero.store import Store
from test_transport_reliability import running


class Clock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class ChapterGapTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".runtime" / "tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".runtime" / "tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def test_geodns_sends_everyone_to_the_surviving_center(self):
        dns = GeoDNS(
            {"us-east": "east-entry", "us-west": "west-entry"},
            {"virginia": "us-east", "oregon": "us-west"},
        )
        self.assertEqual(dns.resolve("virginia"), {"datacenter": "us-east", "entry": "east-entry"})
        self.assertEqual(dns.resolve("oregon")["datacenter"], "us-west")
        first = dns.resolve("visitor-9")
        self.assertEqual(dns.resolve("visitor-9"), first)
        dns.set_health("us-east", False)
        for location in ("virginia", "oregon", "visitor-9"):
            self.assertEqual(dns.resolve(location)["datacenter"], "us-west")
        dns.set_health("us-west", False)
        with self.assertRaises(Error) as caught:
            dns.resolve("virginia")
        self.assertEqual(caught.exception.status, 503)

    def test_geodns_health_flips_stay_inside_known_centers(self):
        dns = GeoDNS(
            {"us-east": "east-entry", "us-west": "west-entry"},
            {"virginia": "us-east"},
        )
        found = []
        lock = threading.Lock()

        def read():
            for _ in range(40):
                choice = dns.resolve("virginia")["datacenter"]
                with lock:
                    found.append(choice)

        def flip():
            for index in range(40):
                dns.set_health("us-east", index % 2 == 0)

        threads = [threading.Thread(target=read) for _ in range(4)]
        threads.append(threading.Thread(target=flip))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(found)
        self.assertTrue(set(found) <= {"us-east", "us-west"})

    def test_live_primary_keeps_the_lease_and_a_lagging_replica_cannot_promote(self):
        path = self.root / "fence.sqlite"
        primary = Fence(path, "primary0", self.clock)
        replica = Fence(path, "replica0", self.clock)
        self.assertEqual(primary.heartbeat(0)["epoch"], 1)
        with self.assertRaises(Error) as caught:
            replica.promote(0)
        self.assertEqual(caught.exception.status, 409)
        primary.publish(1)
        self.clock.now += 2
        with self.assertRaises(Error) as behind:
            replica.promote(0)
        self.assertIn("behind", str(behind.exception))
        self.assertEqual(primary.heartbeat(1)["holder"], "primary0")

    def test_expired_lease_promotes_a_caught_up_replica_and_fences_the_old_primary(self):
        path = self.root / "fence.sqlite"
        primary = Fence(path, "primary0", self.clock)
        replica_fence = Fence(path, "replica0", self.clock)
        primary.heartbeat(0)
        self.clock.now += 2
        promoted = replica_fence.promote(0)
        self.assertEqual(promoted["epoch"], 2)
        self.assertTrue(replica_fence.holds())
        self.assertFalse(primary.holds())
        with self.assertRaises(Error):
            primary.heartbeat(0)
        store = Store(self.root / "replica.sqlite", self.clock)
        store.fence = replica_fence
        written = store.update(
            "alice",
            {
                "request_id": "write-1",
                "expected_version": 0,
                "display_name": "Synthetic User",
                "city": "Test",
            },
        )
        self.assertEqual(written["version"], 1)
        self.assertEqual(replica_fence.status()["cursor"], 1)
        old = Store(self.root / "primary.sqlite", self.clock)
        old.fence = primary
        with self.assertRaises(Error) as refused:
            old.update(
                "alice",
                {
                    "request_id": "write-2",
                    "expected_version": 0,
                    "display_name": "Other",
                    "city": "Test",
                },
            )
        self.assertEqual(refused.exception.status, 403)

    def test_promoted_replica_serves_profile_writes_but_not_sessions(self):
        config = configuration("balanced")
        user = next(iter(config["users"]))
        store = Store(self.root / "replica.sqlite", self.clock)
        store.fence = Fence(self.root / "fence.sqlite", "replica0", self.clock)
        self.clock.now += 1
        instance = server("replica", store, config, 0, "127.0.0.1", 0)
        with running(instance) as running_server:
            base = "http://127.0.0.1:" + str(running_server.server_port)
            promoted = request(
                base + "/internal/promote", config["internal_token"], {}
            )
            self.assertEqual(promoted["holder"], "replica0")
            written = request(
                base + "/internal/update",
                config["internal_token"],
                {
                    "user": user,
                    "profile": {
                        "request_id": "write-1",
                        "expected_version": 0,
                        "display_name": "Synthetic User",
                        "city": "Test",
                    },
                },
            )
            self.assertEqual(written["user"], user)
            with self.assertRaises(Error) as session:
                request(
                    base + "/internal/session_create",
                    config["internal_token"],
                    {"user": user},
                )
            self.assertEqual(session.exception.status, 403)

    def test_eviction_policies_and_chapter_set_get(self):
        now = [0]

        def clock():
            return now[0]

        fifo = Cache(capacity=2, ttl=30, clock=clock, policy="fifo")
        fifo.put("a", {"version": 1})
        fifo.put("b", {"version": 1})
        fifo.get("a")
        fifo.put("c", {"version": 1})
        self.assertIsNone(fifo.get("a"))
        self.assertEqual(fifo.get("b")["version"], 1)

        lfu = Cache(capacity=2, ttl=30, clock=clock, policy="lfu")
        lfu.put("a", {"version": 1})
        lfu.put("b", {"version": 1})
        lfu.get("a")
        lfu.put("c", {"version": 1})
        self.assertEqual(lfu.get("a")["version"], 1)
        self.assertIsNone(lfu.get("b"))
        with self.assertRaises(ValueError):
            Cache(policy="random")

        memo = Cache(ttl=10, clock=clock)
        self.assertTrue(memo.set("myKey", "hi there", 3600))
        self.assertEqual(memo.get("myKey"), "hi there")
        now[0] = 3600
        self.assertIsNone(memo.get("myKey"))

    def test_cache_cluster_skips_a_failed_member(self):
        now = [0]
        live = Cache(clock=lambda: now[0], ttl=30)

        class Down:
            def get(self, key, min_version=0):
                raise OSError("cache down")

            def put(self, key, value, generation=None):
                raise OSError("cache down")

            def delete(self, key, min_version=0):
                raise OSError("cache down")

            def set(self, key, value, ttl):
                raise OSError("cache down")

        cluster = CacheCluster([Down(), live])
        self.assertTrue(cluster.set("myKey", "hi there", 60))
        self.assertEqual(cluster.get("myKey"), "hi there")
        self.assertGreaterEqual(cluster.errors, 1)
        cluster.delete("myKey")
        self.assertIsNone(cluster.get("myKey"))
