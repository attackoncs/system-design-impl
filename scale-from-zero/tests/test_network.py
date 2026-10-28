from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait
from scale_from_zero.common import Error


class NetworkTests(unittest.TestCase):
    def test_single_process_session_data_and_restart(self):
        with Lab("single") as lab:
            lab.login()
            lab.update()
            self.assertEqual(lab.call("/v1/profile")["version"], 1)
            wait(
                lambda: (
                    lab.call("/admin/status", node="single", auth="admin")[
                        "pending_jobs"
                    ]
                    == 0
                )
            )
            lab.stop("single")
            lab.start("single")
            value = lab.call("/v1/profile?consistency=strong")
            self.assertEqual((value["version"], value["source"]), (1, "primary"))

    def test_balanced_stateless_sessions_cache_and_web_failover(self):
        with Lab("balanced") as lab:
            lab.login()
            lab.update()
            lab.caught_up()
            first = lab.call("/v1/profile")
            second = lab.call("/v1/profile")
            self.assertNotEqual(first["web_node"], second["web_node"])
            self.assertEqual(second["source"], "cache")
            self.assertGreaterEqual(
                lab.call("/admin/status", node="cache", auth="admin")["hits"], 1
            )
            reads = lab.call("/admin/status", node="primary0", auth="admin")[
                "profile_reads"
            ]
            for _ in range(4):
                self.assertEqual(lab.call("/v1/profile")["version"], 1)
            self.assertEqual(
                lab.call("/admin/status", node="primary0", auth="admin")[
                    "profile_reads"
                ],
                reads,
            )
            lab.stop("web0")
            for _ in range(4):
                self.assertEqual(lab.call("/v1/profile")["web_node"], "web1")
            self.assertGreater(
                lab.call("/admin/status", node="lb", auth="admin")["failovers"], 0
            )
            lab.stop("cache")
            self.assertEqual(lab.call("/v1/profile")["version"], 1)
            lab.start("cache")
            self.assertEqual(lab.call("/v1/profile?min_version=1")["version"], 1)

    def test_sharded_routing_replica_failure_and_primary_restart(self):
        with Lab("sharded") as lab:
            users = list(lab.config["users"])
            for user in users:
                lab.login(user)
                lab.update(user=user)
            lab.caught_up()
            for index in range(2):
                self.assertEqual(
                    lab.call(
                        "/admin/status", node="primary" + str(index), auth="admin"
                    )["users"],
                    1,
                )
                self.assertEqual(
                    lab.call(
                        "/admin/status", node="replica" + str(index), auth="admin"
                    )["users"],
                    1,
                )
            lab.stop("replica0")
            lab.stop("cache")
            value = lab.call("/v1/profile?min_version=1")
            self.assertEqual(value["source"], "primary")
            lab.update(1, "second")
            lab.start("replica0")
            lab.caught_up()
            self.assertEqual(lab.call("/v1/profile?min_version=2")["version"], 2)
            lab.start("cache")
            lab.stop("primary0")
            with self.assertRaises(Error) as error:
                lab.call("/v1/profile?consistency=strong")
            self.assertEqual(error.exception.status, 503)
            self.assertEqual(
                lab.call("/v1/profile?consistency=strong", user=users[1])["version"], 1
            )
            lab.update(1, "unaffected_shard", user=users[1])
            wait(
                lambda: (
                    lab.call("/admin/status", node="primary1", auth="admin")[
                        "pending_jobs"
                    ]
                    == 0
                )
            )
            lab.start("primary0")
            self.assertEqual(lab.call("/v1/profile?consistency=strong")["version"], 2)

    def test_durable_jobs_lost_ack_and_local_edge_model(self):
        with Lab("balanced") as lab:
            lab.call("/admin/control", {"lose_ack": True}, node="worker", auth="admin")
            lab.login()
            lab.update()
            wait(
                lambda: (
                    lab.call("/admin/status", node="worker", auth="admin")["attempts"]
                    >= 2
                )
            )
            lab.call("/admin/control", {"lose_ack": False}, node="worker", auth="admin")
            lab.stop("worker")
            lab.start("worker")
            lab.drained()
            self.assertEqual(
                lab.call("/admin/status", node="worker", auth="admin")["unique_events"],
                1,
            )
            self.assertEqual(lab.update()["version"], 1)
            first = lab.call("/assets/app-v1.txt", node="edge")
            second = lab.call("/assets/app-v1.txt", node="edge")
            self.assertEqual(first, second)
            self.assertGreater(
                lab.call("/admin/status", node="edge", auth="admin")["hits"], 0
            )
            self.assertNotEqual(
                first["content"], lab.call("/assets/app-v2.txt", node="edge")["content"]
            )
            lab.stop("edge")
            self.assertEqual(
                lab.call("/assets/app-v1.txt")["content"], first["content"]
            )


if __name__ == "__main__":
    unittest.main()
