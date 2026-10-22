from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from scale_from_zero.cache import Cache
from scale_from_zero.common import Error
from scale_from_zero.store import Consumer,Store


class ComponentTests(unittest.TestCase):
    def setUp(self):
        (ROOT/".runtime"/"tmp").mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/".runtime"/"tmp")
        self.root=Path(self.temp.name)
        self.now=1000
        self.store=Store(self.root/"primary.sqlite",lambda:self.now)

    def tearDown(self):
        self.temp.cleanup()

    def body(self,key="write",version=0):
        return {"request_id":key,"expected_version":version,"display_name":"Synthetic User","city":"Test"}

    def test_concurrent_idempotency_and_compare_swap(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            values=list(pool.map(lambda _:self.store.update("alice",self.body()),range(12)))
        self.assertTrue(all(value["version"]==1 for value in values))
        def compete(index):
            try:
                return self.store.update("alice",self.body("next_"+str(index),1))
            except Error:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(compete,range(12)))
        self.assertEqual(sum(value is not None for value in results),1)
        self.assertEqual(self.store.status()["seq"],2)
        self.assertEqual(self.store.status()["pending_jobs"],2)
        with self.assertRaises(Error):
            self.store.update("alice",{**self.body(),"city":"changed"})

    def test_session_persistence_and_expiry(self):
        token=self.store.session_create("alice")["session"]
        self.store=Store(self.root/"primary.sqlite",lambda:self.now)
        self.assertEqual(self.store.session_check("alice",token),{"user":"alice"})
        with self.assertRaises(Error):
            self.store.session_check("bob",token)
        self.now+=3601
        with self.assertRaises(Error):
            self.store.session_check("alice",token)

    def test_partition_map_cannot_change_without_migration(self):
        self.store.bind_partition(1,0)
        self.store=Store(self.root/"primary.sqlite")
        self.store.bind_partition(1,0)
        with self.assertRaises(ValueError):
            self.store.bind_partition(2,0)

    def test_replica_replay_checksum_gap_and_idempotency(self):
        self.store.update("alice",self.body())
        self.store.update("alice",self.body("second",1))
        replica=Store(self.root/"replica.sqlite")
        changes=self.store.changes(0)["changes"]
        with self.assertRaises(Error):
            replica.replicate(changes[1])
        for change in changes:
            replica.replicate(change)
            replica.replicate(change)
        self.assertEqual(replica.get("alice"),self.store.get("alice"))
        with self.assertRaises(Error):
            replica.replicate({**changes[0],"version":100})
        self.assertEqual(replica.status()["pending_jobs"],0)

    def test_cache_lru_capacity_and_copy_isolation(self):
        cache=Cache(capacity=2,clock=lambda:self.now)
        cache.put("a",{"version":1})
        cache.put("b",{"version":1})
        value=cache.get("a")
        value["version"]=99
        cache.put("c",{"version":1})
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a")["version"],1)
        self.assertEqual(cache.status()["evictions"],1)

    def test_cache_ttl_and_minimum_version(self):
        cache=Cache(ttl=2,clock=lambda:self.now)
        cache.put("alice",{"version":1})
        self.assertIsNone(cache.get("alice",2))
        self.now+=2
        self.assertIsNone(cache.get("alice"))
        self.assertEqual(cache.status()["entries"],0)

    def test_worker_lost_ack_dedup_and_lease_fencing(self):
        self.store.update("alice",self.body())
        consumer=Consumer(self.root/"consumer.sqlite")
        first=self.store.claim()
        consumer.control(True)
        self.assertFalse(consumer.consume(first))
        self.now+=2
        newer=self.store.claim()
        self.assertFalse(self.store.ack(first)["acknowledged"])
        consumer.control(False)
        self.assertTrue(consumer.consume(newer))
        self.assertTrue(self.store.ack(newer)["acknowledged"])
        self.assertEqual(consumer.status(),{"unique_events":1,"attempts":2})
        self.assertEqual(self.store.status()["pending_jobs"],0)


if __name__=="__main__":
    unittest.main()
