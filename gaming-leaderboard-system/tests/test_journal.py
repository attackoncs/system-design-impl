from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from gaming_leaderboard.common import Error,digest,month_bounds
from gaming_leaderboard.journal import Journal


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.now=stamp("2030-01-01T12:00:00")
        self.store=Journal(self.root/"journal.sqlite",clock=lambda:self.now)
        self.store.register("alice","Alice")
        self.store.register("bob","Bob")

    def tearDown(self):
        self.temp.cleanup()

    def body(self,id="m",**kwargs):
        return {"match_id":id,"winner":"alice","players":["alice","bob"],"ended_at":self.now,**kwargs}

    def test_exactly_once_concurrent_and_conflict(self):
        body=self.body()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.store.ingest(body),range(20)))
        self.assertEqual(sum(not r["duplicate"] for r in results),1)
        self.assertEqual(self.store.rows("2030-01")[0]["score"],1)
        with self.assertRaises(Error):
            self.store.ingest(self.body(winner="bob"))
        self.assertEqual(self.store.status()["matches"],1)

    def test_precommit_failure_and_reopen_feed_checksums(self):
        def crash():
            raise RuntimeError("before journal commit")
        heads=self.store.status()["heads"]
        with self.assertRaises(RuntimeError):
            self.store.ingest(self.body(),crash)
        self.assertEqual(self.store.status()["heads"],heads)
        self.store.ingest(self.body())
        lineage=self.store.lineage
        self.store=Journal(self.store.path,clock=lambda:self.now)
        self.assertEqual(self.store.lineage,lineage)
        self.assertTrue(self.store.ingest(self.body())["duplicate"])
        for shard in range(2):
            feed=self.store.feed(shard,0)
            self.assertEqual([e["seq"] for e in feed["events"]],list(range(1,feed["head"]+1)))
            self.assertTrue(all(digest(e["payload"])==e["hash"] for e in feed["events"]))

    def test_month_boundary_late_acceptance_settlement_and_reset(self):
        self.now=stamp("2030-01-31T23:59:59")
        body=self.body()
        self.store.ingest(body)
        self.now=stamp("2030-02-01T00:00:01")
        self.store.ingest({**body,"match_id":"late"})
        self.assertEqual(self.store.rows("2030-01")[0]["score"],2)
        self.assertEqual(self.store.rows("2030-02")[0]["score"],0)
        with self.assertRaises(Error):
            self.store.close("2030-01")
        self.now=month_bounds("2030-01")[1]+172800
        settled=self.store.close("2030-01")
        self.assertEqual(settled["top10"][0]["score"],2)
        self.assertEqual(self.store.close("2030-01"),settled)
        self.assertTrue(self.store.ingest(body)["duplicate"])
        with self.assertRaises(Error):
            self.store.ingest({**body,"match_id":"too-late"})

    def test_all_registered_including_zero_ties_and_profiles(self):
        self.store.register("charlie","Charlie")
        rows=self.store.rows("2030-01")
        self.assertEqual([r["user"] for r in rows],["alice","bob","charlie"])
        self.assertTrue(all(r["score"]==0 for r in rows))
        self.store.register("alice","Updated")
        self.assertEqual(self.store.names(["alice"]),{"alice":"Updated"})
        self.now=month_bounds("2030-01")[1]+172800
        self.assertEqual([r["rank"] for r in self.store.close("2030-01")["top10"]],[1,1,1])

    def test_invalid_and_untrusted_result_shape(self):
        for body in [self.body(points=99),self.body(ended_at=True),self.body(winner="unknown"),
                     self.body(players=["alice","alice"]),self.body(ended_at=self.now+31)]:
            with self.assertRaises((ValueError,Error)):
                self.store.ingest(body)
        self.now+=10
        self.store.register("new","New")
        with self.assertRaises(Error):
            self.store.ingest(self.body("past",winner="new",players=["new","bob"],ended_at=self.now-5))
        with self.assertRaises(Error):
            Journal(self.store.path,shards=1)


if __name__=="__main__":
    unittest.main()
