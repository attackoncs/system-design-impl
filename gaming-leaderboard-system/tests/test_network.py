from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time
import unittest
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait_for
from gaming_leaderboard.common import current_month, lex_member


class NetworkTests(unittest.TestCase):
    def test_ties_global_rank_neighbors_and_authenticated_results(self):
        with Lab() as lab:
            users = [f"user{i:02}" for i in range(14)] + ["zz_loser"]
            for user in users:
                lab.register(user)
            expected = {user: 0 for user in users}
            number = 0
            for user, wins in [
                (users[0], 3),
                (users[1], 2),
                (users[2], 2),
                (users[3], 1),
            ]:
                for _ in range(wins):
                    lab.match(f"match-{number}", user, "zz_loser")
                    expected[user] += 1
                    number += 1
            wait_for(lab.caught_up)
            ordered = sorted(expected, key=lambda u: (-expected[u], u))
            top = lab.api("/v1/scores")["rows"]
            self.assertEqual([r["user"] for r in top], ordered[:10])
            self.assertEqual([r["rank"] for r in top], [1, 2, 2, 4, 5, 5, 5, 5, 5, 5])
            target = users[7]
            window = lab.api(f"/v1/scores/{target}?neighbors=4")["rows"]
            position = ordered.index(target)
            self.assertEqual(
                [r["user"] for r in window], ordered[position - 4 : position + 5]
            )
            for row in window:
                self.assertEqual(
                    row["rank"], 1 + sum(v > row["score"] for v in expected.values())
                )
                self.assertEqual(row["position"], ordered.index(row["user"]) + 1)
            body = {
                "match_id": "duplicate",
                "winner": users[0],
                "players": [users[0], "zz_loser"],
                "ended_at": int(time.time()),
            }
            with ThreadPoolExecutor(max_workers=4) as pool:
                receipts = list(
                    pool.map(lambda _: lab.api("/v1/scores", body, "game"), range(8))
                )
            self.assertEqual(sum(not r["duplicate"] for r in receipts), 1)
            self.assertEqual(lab.api("/v1/scores/" + users[0])["rows"][0]["score"], 4)
            with self.assertRaises(HTTPError) as untrusted:
                lab.api("/v1/scores", body, "read")
            self.assertEqual(untrusted.exception.code, 401)
            with self.assertRaises(HTTPError) as points:
                lab.api(
                    "/v1/scores", {**body, "match_id": "bad", "points": 999}, "game"
                )
            self.assertEqual(points.exception.code, 400)
            self.assertTrue(
                lab.api(
                    "/admin/reconcile", {"month": current_month(time.time())}, "admin"
                )["matches"]
            )

    def test_real_primary_crash_manual_promotion_and_aof_restart(self):
        with Lab() as lab:
            lab.register("alice")
            lab.register("loser")
            lab.match("before", "alice")
            root = lab.config["namespace"] + ":shard:0"
            head = lab.api("/internal/status", role="internal")["heads"]["0"]
            wait_for(
                lambda: (
                    int(lab.clients[1].command("GET", root + ":position") or 0) >= head
                )
            )
            lab.stop_redis(0, crash=True)
            with self.assertRaises(HTTPError) as unavailable:
                lab.api("/v1/scores/alice")
            self.assertEqual(unavailable.exception.code, 503)
            lab.clients[1].command("REPLICAOF", "NO", "ONE")
            wait_for(lambda: lab.api("/v1/scores/alice")["rows"][0]["score"] == 1)
            lab.match("after", "alice")
            self.assertEqual(lab.api("/v1/scores/alice")["rows"][0]["score"], 2)
            lab.spawn_redis(0, follow=1)
            wait_for(
                lambda: (
                    int(lab.clients[0].command("GET", root + ":position") or 0)
                    >= head + 1
                )
            )
            # Reopen the promoted master's real AOF, with its peer still a replica.
            lab.stop("projection-0")
            lab.stop_redis(1, crash=True)
            lab.spawn_redis(1, follow=-1)
            month = current_month(time.time())
            self.assertEqual(
                float(
                    lab.clients[1].command("ZSCORE", root + ":score:" + month, "alice")
                ),
                -2,
            )
            lab.spawn("projection-0")
            wait_for(lambda: lab.api("/v1/scores/alice")["rows"][0]["score"] == 2)
            self.assertTrue(
                lab.api(
                    "/admin/reconcile", {"month": current_month(time.time())}, "admin"
                )["matches"]
            )

    def test_redis_loss_journal_rebuild_and_corruption_reconciliation(self):
        with Lab() as lab:
            for user in ("alice", "loser", "player"):
                lab.register(user)
            lab.match("win-1", "alice")
            lab.match("win-2", "alice")
            month = current_month(time.time())
            root = lab.config["namespace"] + ":shard:0"
            # This Redis belongs entirely to this isolated lab, never a user's instance.
            lab.clients[0].command("FLUSHALL")
            wait_for(lab.caught_up)
            self.assertEqual(lab.api("/v1/scores/alice")["rows"][0]["score"], 2)
            lab.stop("authority")
            lab.spawn("authority")
            self.assertEqual(lab.api("/v1/scores/alice")["rows"][0]["score"], 2)
            # Corrupt both indexes consistently so the independent audit, rather
            # than cardinality checking, has to detect a wrong ranking.
            lab.clients[0].command("ZADD", root + ":score:" + month, -99, "alice")
            lab.clients[0].command(
                "ZREM", root + ":order:" + month, lex_member("alice", 2)
            )
            lab.clients[0].command(
                "ZADD", root + ":order:" + month, 0, lex_member("alice", 99)
            )
            self.assertFalse(
                lab.api("/admin/reconcile", {"month": month}, "admin")["matches"]
            )
            lab.api("/admin/rebuild", {}, "admin")
            wait_for(lab.caught_up)
            self.assertTrue(
                lab.api("/admin/reconcile", {"month": month}, "admin")["matches"]
            )
            self.assertEqual(lab.api("/v1/scores/alice")["rows"][0]["score"], 2)


if __name__ == "__main__":
    unittest.main()
