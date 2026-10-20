import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
from ad_click.aggregate import Aggregator
from ad_click.common import Error, digest, dumps, http_json
from ad_click.service import aggregate_query, reconcile, replay
from ad_click.tailer import ingest_file
from lab import Lab, wait_for


def click(i, ad="hot", country="US", timestamp=None):
    return {"event_id": str(i), "ad_id": ad, "click_timestamp": timestamp or int(time.time()) // 60 * 60 - 30,
            "user_id": "user1", "ip": "127.0.0.1", "country": country}


class NetworkTests(unittest.TestCase):
    def test_global_merge_cannot_truncate_local_top_n(self):
        with Lab() as lab:
            counts = {(p, ad): 0 for p in range(4) for ad in ("hot", "local" + str(p))}
            values = []
            for i in range(10000):
                value = click("candidate" + str(i))
                p = int(digest(value["event_id"])[:16], 16) % 4
                ad = "hot" if counts[p, "hot"] < 6 else "local" + str(p)
                if counts[p, ad] >= (6 if ad == "hot" else 9):
                    continue
                value["ad_id"] = ad
                counts[p, ad] += 1
                values.append(value)
                if len(values) == 60:
                    break
            lab.send(values)
            wait_for(lab.caught_up)
            # Every local top-1 is localP:9, yet the global winner is hot:24.
            self.assertEqual(lab.query({"window": 2, "count": 1})["ads"], [{"ad_id": "hot", "count": 24}])

    def test_real_pipeline_filters_global_top_and_auth(self):
        with Lab() as lab:
            end = int(time.time()) // 60 * 60
            events = [click(i, timestamp=end-30) for i in range(120)] + [click("cold" + str(i), "cold", "GB", timestamp=end-30) for i in range(45)]
            lab.send(events)
            lab.send(events)
            wait_for(lab.caught_up)
            body = {"from": end - 60, "to": end, "count": 1}
            result = lab.query(body)
            self.assertEqual(result["ads"], [{"ad_id": "hot", "count": 120}])
            self.assertEqual(lab.query({**body, "filters": {"country": "GB"}})["ads"], [{"ad_id": "cold", "count": 45}])
            self.assertEqual(lab.query({**body, "ad_id": "missing"})["count"], 0)
            self.assertEqual(lab.call("api", f"/v1/ads/hot/aggregated_count?from={end-60}&to={end}&user_id=user1", role="read")["count"], 120)
            for name, path, token in [("ingress", "/internal/log?partition=0&after=0", "read"),
                                      ("api", "/v1/ads/popular_ads", "write")]:
                with self.assertRaises(HTTPError) as error:
                    lab.call(name, path, role=token)
                self.assertEqual(error.exception.code, 401)
            self.assertTrue(reconcile(lab.config)["matches"])
            with self.assertRaises(Error):
                aggregate_query({**lab.config, "worker_urls": lab.config["worker_urls"][:-1]}, body)

    def test_worker_crash_before_commit_recovery_and_ingress_restart(self):
        with Lab() as lab:
            lab.stop("worker-0")
            selected = []
            for i in range(1000):
                value = click("crash" + str(i))
                if int(digest(value["event_id"])[:16], 16) % 4 == 0:
                    selected.append(value)
                if len(selected) == 25:
                    break
            receipt = lab.send(selected)
            with self.assertRaises(HTTPError) as error:
                lab.query({})
            self.assertEqual(error.exception.code, 503)  # Never return a partial Top N.
            code = """import json,os,sys
from ad_click.aggregate import Aggregator
from ad_click.service import log_page
c=json.load(open(sys.argv[1])); w=Aggregator(sys.argv[2],0)
w.apply(log_page(c,0,0),lambda:os._exit(73))
"""
            process = subprocess.run([sys.executable, "-c", code, str(lab.config_path), str(lab.root / "worker-0.sqlite")],
                                     env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), timeout=15, capture_output=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(process.returncode, 73, process.stderr)
            store = Aggregator(lab.root / "worker-0.sqlite", 0)
            self.assertEqual(store.status()["seq"], 0)
            self.assertEqual(store.audit()["count"], 0)
            lab.spawn("worker-0")
            wait_for(lab.caught_up)
            self.assertEqual(lab.query({"window": 2, "ad_id": "hot"})["count"], 25)
            lab.stop("ingress")
            lab.spawn("ingress")
            repeated = lab.send(selected)
            self.assertTrue(all(r["duplicate"] for r in repeated["receipts"]))
            self.assertEqual(receipt["lineage"], repeated["lineage"])
            wait_for(lab.caught_up)
            self.assertEqual(lab.query({"window": 2, "ad_id": "hot"})["count"], 25)
            self.assertTrue(reconcile(lab.config)["matches"])

    def test_jsonl_partial_record_late_correction_rebuild_and_reconciliation(self):
        with Lab() as lab:
            source, checkpoint = lab.root / "events.jsonl", lab.root / "tail.json"
            old = int(time.time()) // 60 * 60 - 600
            first, second = click("late1", timestamp=old + 1), click("late2", timestamp=old + 2)
            raw = (dumps(first) + "\n" + dumps(second)).encode()
            source.write_bytes(raw)
            self.assertEqual(ingest_file(lab.config, source, checkpoint)["records"], 1)
            self.assertEqual(ingest_file(lab.config, source, checkpoint)["records"], 0)
            with source.open("ab") as handle:
                handle.write(b"\n")
            self.assertEqual(ingest_file(lab.config, source, checkpoint)["records"], 1)
            wait_for(lab.caught_up)
            result = lab.query({"from": old, "to": old + 60, "ad_id": "hot"})
            self.assertEqual(result["count"], 2)
            self.assertTrue(result["closed"])
            lab.send([click("late3", timestamp=old + 3)])
            wait_for(lab.caught_up)
            updated = lab.query({"from": old, "to": old + 60, "ad_id": "hot"})
            self.assertEqual(updated["count"], 3)
            self.assertNotEqual(result["checkpoints"], updated["checkpoints"])
            for p in range(4):
                shadow = Aggregator(lab.root / f"shadow-{p}.sqlite", p)
                replay(lab.config, shadow)
                live = lab.call(f"worker-{p}", "/internal/audit")
                self.assertEqual(shadow.audit()["digest"], live["digest"])
            self.assertTrue(reconcile(lab.config)["matches"])
            # Deliberately corrupt only an aggregate, leaving the immutable raw log intact.
            p = next(p for p in range(4) if lab.call(f"worker-{p}", "/internal/audit")["count"])
            store = Aggregator(lab.root / f"worker-{p}.sqlite", p)
            with store.transaction(True) as db:
                db.execute("UPDATE buckets SET count=count+1")
            self.assertFalse(reconcile(lab.config)["matches"])
            source.write_bytes(b"changed\n")
            with self.assertRaises(Exception):
                ingest_file(lab.config, source, checkpoint)


if __name__ == "__main__":
    unittest.main()
