import asyncio
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tools")]
from nearby_friends.backend import Backend
from nearby_friends.core import Ring
from lab import Client, Cluster


def delta(kind, identifier, revision=None):
    return lambda message: (
        message.get("type") == "delta"
        and any(
            change["event"] == kind
            and (
                change.get("id") == identifier
                or change.get("friend", {}).get("id") == identifier
            )
            and (
                revision is None or change.get("friend", {}).get("revision") == revision
            )
            for change in message["changes"]
        )
    )


@unittest.skipUnless(
    os.environ.get("NEARBY_REDIS_BINARY") or os.environ.get("NEARBY_WSL_REDIS_BINARY"),
    "real Redis binary required for network suite",
)
class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cluster = Cluster(
            ttl=60 if "ring_reconfiguration" in self._testMethodName else 3
        )
        self.clients = []
        try:
            self.cluster.start_redis()
            self.urls = [
                await self.cluster.start_node(name, reconcile=0.2)
                for name in ["ws-a", "ws-b"]
            ]
            self.alice = await self.open("alice", 0)
            self.bob = await self.open("bob", 1)
            self.eve = await self.open("eve", 1)
            for client in self.clients:
                self.assertEqual(
                    (await client.command("sharing", enabled=True))["type"], "ack"
                )
        except BaseException:
            await self.asyncTearDown()
            raise

    async def open(self, user, node):
        client = await Client.open(self.urls[node], self.cluster.tokens[user])
        self.clients.append(client)
        return client

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.cluster.close()

    async def friend_and_locate(self):
        await self.alice.command("friend.request", target="bob")
        await self.bob.command("friend.accept", target="alice")
        await self.alice.command(
            "location", latitude=0, longitude=179.999, timestamp=time.time()
        )
        return await self.bob.command(
            "location", latitude=0, longitude=-179.999, timestamp=time.time()
        )

    async def test_cross_node_entry_exit_privacy_order_and_history(self):
        ack = await self.friend_and_locate()
        await self.alice.until(delta("enter", "bob", ack["revision"]))
        await asyncio.sleep(0.7)
        ring = Ring(self.cluster.store.ring()["nodes"])
        owner = 0 if ring.owner("bob") == "broker-a" else 1
        channel = self.cluster.namespace + ":user:bob"
        self.assertGreater(self.cluster.redis[owner][0].pubsub_numsub(channel)[0][1], 0)
        self.assertEqual(
            self.cluster.redis[1 - owner][0].pubsub_numsub(channel)[0][1], 0
        )
        self.assertGreater(
            self.cluster.redis[0][0].pttl(self.cluster.namespace + ":location:bob"), 0
        )
        rows = (await self.alice.command("snapshot"))["friends"]
        self.assertEqual([r["id"] for r in rows], ["bob"])
        self.assertAlmostEqual(rows[0]["distance_m"], 222.39016, places=4)
        self.assertNotIn("device", rows[0])
        await self.eve.command(
            "location", latitude=0, longitude=179.999, timestamp=time.time()
        )
        self.assertEqual((await self.eve.command("snapshot"))["total"], 0)
        self.assertEqual(
            (await self.eve.command("friend.accept", target="alice"))["code"], 403
        )
        stale = await self.bob.command(
            "location", latitude=0, longitude=0, timestamp=time.time() - 10
        )
        self.assertEqual(
            stale["code"], 400
        )  # TTL=3 rejects old timestamps before ordering.
        await self.bob.command(
            "location", latitude=1, longitude=1, timestamp=time.time()
        )
        await self.alice.until(delta("leave", "bob"))
        await self.bob.command(
            "location", latitude=0, longitude=-179.999, timestamp=time.time()
        )
        await self.alice.until(delta("enter", "bob"))
        self.assertEqual((await self.bob.command("history"))["history"], [])
        await self.bob.command("history.consent", enabled=True)
        await self.bob.command(
            "location", latitude=0, longitude=-179.998, timestamp=time.time()
        )
        self.assertEqual(len((await self.bob.command("history"))["history"]), 1)
        self.assertEqual(
            (await self.alice.command("history", target="bob"))["history"], []
        )
        await self.bob.command("sharing", enabled=False)
        await self.alice.until(delta("leave", "bob"))
        self.assertEqual((await self.alice.command("snapshot"))["total"], 0)
        await self.bob.command("sharing", enabled=True)
        self.assertEqual((await self.alice.command("snapshot"))["total"], 0)
        await self.bob.command(
            "location", latitude=0, longitude=-179.999, timestamp=time.time()
        )
        await self.alice.until(delta("enter", "bob"))
        await self.alice.command("friend.remove", target="bob")
        self.assertEqual((await self.alice.command("snapshot"))["total"], 0)
        await self.bob.command("history.consent", enabled=False)
        self.assertEqual((await self.bob.command("history"))["history"], [])

    async def test_reconnect_ttl_and_pubsub_loss_recovery(self):
        await self.friend_and_locate()
        await self.alice.close()
        self.clients.remove(self.alice)
        self.alice = await self.open("alice", 0)
        self.assertEqual(self.alice.initial["total"], 1)
        # Deliberately commit/cache without publishing: simulate a lost Pub/Sub hint.
        backend = Backend(
            self.cluster.store, self.cluster.urls[0], self.cluster.namespace
        )
        try:
            report = self.cluster.store.update(
                "bob", {"latitude": 0, "longitude": -179.998, "timestamp": time.time()}
            )
            backend.warm(report)
        finally:
            backend.close()
        await self.alice.until(delta("update", "bob", report["revision"]))
        await self.alice.until(delta("leave", "bob"), timeout=5)
        self.assertEqual((await self.alice.command("snapshot"))["total"], 0)

    async def test_ring_reconfiguration_real_broker_failure_and_paging(self):
        await self.friend_and_locate()
        self.assertEqual((await self.alice.command("snapshot", limit=1))["total"], 1)
        self.assertEqual(
            (await self.alice.command("snapshot", offset=1, limit=1))["friends"], []
        )
        self.assertEqual(
            (
                await self.eve.command(
                    "ring", nodes={"b": self.cluster.urls[1]}, expected_epoch=1
                )
            )["code"],
            403,
        )
        changed = await self.alice.command(
            "ring", nodes={"broker-b": self.cluster.urls[1]}, expected_epoch=1
        )
        self.assertEqual(changed["epoch"], 2)
        await asyncio.sleep(0.7)
        expanded = await self.alice.command(
            "ring",
            nodes={"broker-a": self.cluster.urls[0], "broker-b": self.cluster.urls[1]},
            expected_epoch=2,
        )
        self.assertEqual(expanded["epoch"], 3)
        await asyncio.sleep(0.7)
        await self.alice.command(
            "ring", nodes={"broker-b": self.cluster.urls[1]}, expected_epoch=3
        )
        await asyncio.sleep(0.7)
        self.cluster.stop_redis(0)
        ack = await self.bob.command(
            "location", latitude=0, longitude=-179.998, timestamp=time.time()
        )
        self.assertTrue(ack["hint_published"])
        await self.alice.until(delta("update", "bob", ack["revision"]), timeout=8)
        self.assertEqual((await self.alice.command("snapshot"))["total"], 1)


@unittest.skipUnless(
    os.environ.get("NEARBY_REDIS_BINARY") or os.environ.get("NEARBY_WSL_REDIS_BINARY"),
    "real Redis binary required for cache Lua suite",
)
class RedisCacheTests(unittest.TestCase):
    def setUp(self):
        self.cluster = Cluster(ttl=60)
        self.addCleanup(self.cluster.close)
        self.cluster.start_redis()
        self.cluster.store.setting("bob", "sharing", True)
        self.backend = Backend(
            self.cluster.store, self.cluster.urls[0], self.cluster.namespace
        )
        self.addCleanup(self.backend.close)
        self.redis = self.cluster.redis[0][0]
        self.key = self.backend.key("bob")

    def report(self, timestamp):
        return self.cluster.store.update(
            "bob", {"latitude": 0, "longitude": 0, "timestamp": timestamp}
        )

    def test_delayed_warm_cannot_replace_newer_revision_or_extend_receipt_ttl(self):
        now = time.time()
        old = self.report(now)
        current = self.report(now + 0.001)
        self.backend.warm(current)
        remaining = self.redis.pttl(self.key)
        self.backend.warm(old)
        self.assertEqual(json.loads(self.redis.get(self.key)), current)
        self.assertGreater(self.redis.pttl(self.key), 0)
        self.assertLessEqual(self.redis.pttl(self.key), remaining)
        self.assertLessEqual(remaining, 60000)

    def test_real_lua_repairs_malformed_values_and_wrong_type_keys(self):
        report = self.report(time.time())
        for value in (b"not-json", b"null", b"true", b"[]", b'{"revision":true}'):
            with self.subTest(value=value):
                self.redis.set(self.key, value)
                self.backend.warm(report)
                self.assertEqual(json.loads(self.redis.get(self.key)), report)
                self.assertGreater(self.redis.pttl(self.key), 0)
        self.redis.delete(self.key)
        self.redis.rpush(self.key, b"wrong-type")
        self.backend.warm(report)
        self.assertEqual(json.loads(self.redis.get(self.key)), report)
        self.assertGreater(self.redis.pttl(self.key), 0)


if __name__ == "__main__":
    unittest.main()
