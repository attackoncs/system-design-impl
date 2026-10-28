import json
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Cluster, request


class NetworkTests(unittest.TestCase):
    def test_real_replication_region_failover_conditional_tiles_and_restart(self):
        with Cluster() as cluster:
            primary, replica, gateway = cluster.start_all()
            options = {"origin": "West Station", "destination": "East Station"}
            before = request(
                gateway, "/v1/route", "POST", options, headers={"X-Region": "local"}
            )
            self.assertEqual(before["routes"][0]["edges"], ["ab", "bc"])
            graph = before["graph"]
            tilepath = f"/v1/tiles/{graph}/1/0/0.png"
            payload, headers = request(
                gateway, tilepath, headers={"X-Region": "local"}, raw=True
            )
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
            with self.assertRaises(HTTPError) as caught:
                request(
                    gateway,
                    tilepath,
                    headers={"X-Region": "local", "If-None-Match": headers["ETag"]},
                    raw=True,
                )
            self.assertEqual(caught.exception.code, 304)
            for region, expected in [("unknown", 400), ("isolated", 503)]:
                with self.assertRaises(HTTPError) as caught:
                    request(
                        gateway,
                        "/v1/route",
                        "POST",
                        options,
                        headers={"X-Region": region},
                    )
                self.assertEqual(caught.exception.code, expected)
            request(
                primary,
                "/internal/traffic",
                "POST",
                {"records": [{"edge": "bc", "speed_mps": 1}]},
                "admin-demo",
            )
            cluster.wait(lambda: request(replica, "/health")["traffic_version"] == 1)
            self.assertEqual(
                request(replica, "/v1/route", "POST", options)["routes"][0]["edges"],
                ["ad", "de", "ec"],
            )
            with self.assertRaises(HTTPError) as caught:
                request(replica, "/v1/locations", "POST", {"reports": []}, "alice-demo")
            self.assertIn(caught.exception.code, (403, 401))
            cluster.stop("primary")
            self.assertEqual(
                request(
                    gateway, "/v1/route", "POST", options, headers={"X-Region": "local"}
                )["traffic_version"],
                1,
            )
            self.assertEqual(
                request(gateway, tilepath, headers={"X-Region": "local"}, raw=True)[0],
                payload,
            )
            cluster.stop("replica")
            restarted = cluster.start(
                "replica",
                "replica",
                [
                    "--upstream",
                    primary,
                    "--token-file",
                    str(cluster.root / "admin.token"),
                ],
            )
            self.assertEqual(
                request(restarted, "/v1/route", "POST", options)["traffic_version"], 1
            )

    def test_http_auth_ingest_replay_validation_and_graph_swap(self):
        with Cluster() as cluster:
            primary = cluster.start("primary", "serve")
            import time

            body = {
                "reports": [
                    {
                        "event_id": "p1",
                        "sequence": 1,
                        "timestamp": time.time(),
                        "latitude": 0,
                        "longitude": -0.001,
                        "heading": 90,
                    }
                ]
            }
            with self.assertRaises(HTTPError) as caught:
                request(primary, "/v1/locations", "POST", body)
            self.assertEqual(caught.exception.code, 401)
            first = request(primary, "/v1/locations", "POST", body, "alice-demo")
            self.assertEqual(
                request(primary, "/v1/locations", "POST", body, "alice-demo"), first
            )
            self.assertEqual(
                request(primary, "/v1/locations", token="bob-demo")["reports"], []
            )
            for path, method, data, token, status in [
                ("/internal/catalog", "GET", None, "alice-demo", 403),
                (
                    "/v1/route",
                    "POST",
                    {"origin": "A", "destination": "C", "mode": "flying"},
                    None,
                    400,
                ),
                ("/v1/reverse?latitude=nan&longitude=0", "GET", None, None, 400),
            ]:
                with self.assertRaises(HTTPError) as caught:
                    request(primary, path, method, data, token)
                self.assertEqual(caught.exception.code, status)
            meta = request(primary, "/health")
            oldpath = f"/v1/tiles/{meta['graph']}/0/0/0.geojson"
            request(primary, oldpath)
            graph = json.loads((ROOT / "examples/roads.json").read_text())
            graph["nodes"][0]["longitude"] = -0.003
            after = request(primary, "/internal/graph", "POST", graph, "admin-demo")
            self.assertNotEqual(after["graph"], meta["graph"])
            with self.assertRaises(HTTPError) as caught:
                request(primary, oldpath)
            self.assertEqual(caught.exception.code, 410)


if __name__ == "__main__":
    unittest.main()
