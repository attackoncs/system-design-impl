import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ad_click.aggregate import Aggregator
from ad_click.common import Error, query
from ad_click.log import EventLog


def click(i, ad="hot", timestamp=601, country="US", user="u", ip="127.0.0.1"):
    return dict(
        event_id=str(i),
        ad_id=ad,
        click_timestamp=timestamp,
        user_id=user,
        ip=ip,
        country=country,
    )


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = lambda: 1000
        self.log = EventLog(self.root / "raw.sqlite", 4, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def drain(self, p=0):
        worker = Aggregator(self.root / f"w{p}.sqlite", p, clock=self.clock)
        worker.apply(self.log.read(p, 0))
        return worker

    def test_log_duplicate_conflict_atomicity_and_restart(self):
        first = self.log.append([click("a")])
        again = self.log.append([click("a")])
        self.assertTrue(again["receipts"][0]["duplicate"])
        self.assertEqual(first["receipts"][0]["seq"], again["receipts"][0]["seq"])
        with self.assertRaises(Error):
            self.log.append([click("b"), click("a", ad="changed")])
        self.assertEqual(self.log.status()["raw_events"], 1)
        reopened = EventLog(self.log.path, 4, clock=self.clock)
        self.assertEqual(reopened.lineage, self.log.lineage)
        self.assertTrue(reopened.append([click("a")])["receipts"][0]["duplicate"])
        with self.assertRaises(Error):
            EventLog(self.log.path, 2)

    def test_validation_and_capacity(self):
        for bad in [
            dict(click(1), click_timestamp=True),
            dict(click(1), ip="bad"),
            dict(click(1), country="USA"),
            dict(click(1), click_timestamp=1031),
            dict(click(1), unexpected=1),
        ]:
            with self.assertRaises(ValueError):
                self.log.append([bad])
        limited = EventLog(
            self.root / "small.sqlite", 1, max_events=1, clock=self.clock
        )
        with self.assertRaises(Error):
            limited.append([click(1), click(2)])
        self.assertEqual(limited.status()["raw_events"], 0)
        limited.append([click(1)])
        limited.append([click(1)])  # Duplicate remains accepted at capacity.
        with self.assertRaises(Error):
            limited.append([click(2)])

    def test_salted_hot_ad_and_global_totals(self):
        self.log.append([click(i) for i in range(200)])
        heads = self.log.status()["heads"]
        self.assertTrue(all(value > 20 for value in heads.values()), heads)
        total = 0
        for p in range(4):
            worker = self.drain(p)
            total += worker.totals({"from": 600, "to": 660})["totals"]["hot"]
        self.assertEqual(total, 200)

    def test_checkpoint_output_rollback_and_replay(self):
        log = EventLog(self.root / "one.sqlite", 1, clock=self.clock)
        log.append([click(1), click(2)])
        worker = Aggregator(self.root / "w.sqlite", 0, clock=self.clock)

        def fail():
            raise RuntimeError("injected before commit")

        with self.assertRaises(RuntimeError):
            worker.apply(log.read(0, 0), fail)
        self.assertEqual(worker.status()["seq"], 0)
        self.assertEqual(worker.audit()["count"], 0)
        worker.apply(log.read(0, 0))
        worker = Aggregator(worker.path, 0, clock=self.clock)
        worker.apply(log.read(0, 0))
        self.assertEqual(worker.audit()["count"], 2)
        self.assertEqual(worker.status()["seq"], 2)

    def test_gap_checksum_and_lineage_fail_closed(self):
        log = EventLog(self.root / "one.sqlite", 1, clock=self.clock)
        log.append([click(1), click(2)])
        worker = Aggregator(self.root / "w.sqlite", 0, clock=self.clock)
        batch = log.read(0, 0)
        corrupt = json.loads(json.dumps(batch))
        corrupt["entries"][0]["event"]["ad_id"] = "bad"
        with self.assertRaises(Error):
            worker.apply(corrupt)
        with self.assertRaises(Error):
            worker.apply({**batch, "entries": batch["entries"][1:]})
        worker.apply(batch)
        with self.assertRaises(Error):
            worker.apply({**batch, "lineage": "other-log"})
        self.assertEqual(worker.audit()["count"], 2)

    def test_late_revision_watermark_and_filters(self):
        log = EventLog(self.root / "one.sqlite", 1, clock=self.clock)
        worker = Aggregator(self.root / "w.sqlite", 0, lateness=120, clock=self.clock)
        log.append([click(1, timestamp=901), click(2, timestamp=902, country="GB")])
        worker.apply(log.read(0, 0))
        worker.idle()
        self.assertFalse(worker.totals({"from": 900, "to": 960})["closed"])
        log.append([click(3, timestamp=601), click(4, timestamp=602)])
        worker.apply(log.read(0, 2))
        self.assertEqual(worker.status()["late"], 2)
        self.assertTrue(worker.totals({"from": 600, "to": 660})["closed"])
        self.assertEqual(
            worker.totals(
                {
                    "from": 600,
                    "to": 960,
                    "filters": {"country": "US", "user_id": "u", "ip": "127.0.0.1"},
                }
            )["totals"],
            {"hot": 3},
        )
        with worker.transaction() as db:
            self.assertEqual(
                db.execute("SELECT revision FROM buckets WHERE minute=600").fetchone()[
                    0
                ],
                2,
            )
        self.assertEqual(worker.totals({"from": 660, "to": 900})["totals"], {})

    def test_query_budget_and_boundaries(self):
        for bad in [
            {"from": 601, "to": 660},
            {"from": 600, "to": 600},
            {"window": 1441},
            {"count": True},
            {"filters": {"unknown": "x"}},
            {"filters": {"country": []}},
            {"other": 1},
        ]:
            with self.assertRaises((ValueError, TypeError)):
                query(bad, 1000)
        self.assertEqual(query({}, 1000)["to"], 960)
        worker = self.drain()
        with worker.transaction(True) as db:
            db.executemany(
                "INSERT INTO buckets VALUES(600,?,'u','127.0.0.1','US',1,1)",
                [(str(i),) for i in range(10001)],
            )
        with self.assertRaises(Error):
            worker.totals({"from": 600, "to": 660})


if __name__ == "__main__":
    unittest.main()
