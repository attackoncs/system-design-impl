from pathlib import Path
import sys
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab, info
from gaming_leaderboard.common import current_month, dumps
from gaming_leaderboard.journal import Journal
from gaming_leaderboard.projection import Projection
from gaming_leaderboard.projection_scripts import APPLY
from gaming_leaderboard.redis import RedisError


class ProjectionNetworkTests(unittest.TestCase):
    def test_real_lua_prevalidation_reset_fence_and_idle_aof(self):
        with Lab() as lab:
            config = {**lab.config, "namespace": lab.config["namespace"] + "_audit"}
            journal = Journal(lab.root / "audit.sqlite")
            journal.register("alice", "Alice")
            journal.register("bob", "Bob")
            projection = Projection(config, 0)
            projection.apply(journal.feed(0, 0))
            position = projection.status()["position"]
            before = int(info(lab.clients[0], "persistence")["aof_current_size"])
            for _ in range(10):
                projection.apply(journal.feed(0, position))
            after = int(info(lab.clients[0], "persistence")["aof_current_size"])
            self.assertEqual(before, after, "idle projection rewrote AOF")
            journal.ingest(
                {
                    "match_id": "win",
                    "winner": "alice",
                    "players": ["alice", "bob"],
                    "ended_at": int(time.time()),
                }
            )
            batch = journal.feed(0, position)
            month = current_month(time.time())
            client = lab.clients[0]
            client.command("DEL", projection.root + ":months")
            client.command("SET", projection.root + ":months", "corrupt")
            with self.assertRaises(RedisError):
                projection.apply(batch)
            self.assertEqual(projection.status()["position"], position)
            self.assertEqual(
                float(
                    client.command(
                        "ZSCORE", projection.root + ":score:" + month, "alice"
                    )
                ),
                0,
            )
            client.command("DEL", projection.root + ":months")
            client.command("SADD", projection.root + ":months", month)
            old = projection.status()
            projection.reset()
            with self.assertRaises(RedisError):
                client.command(
                    "EVAL",
                    APPLY,
                    1,
                    projection.root,
                    dumps(
                        {**batch, "epoch": old["epoch"], "next_epoch": uuid.uuid4().hex}
                    ),
                )
            self.assertEqual(projection.status()["position"], 0)
            projection.apply(journal.feed(0, 0))
            status = projection.status()
            with self.assertRaises(RedisError):
                projection.query(
                    {
                        "month": month,
                        "lineage": journal.lineage,
                        "minimum": status["position"],
                        "epoch": old["epoch"],
                        "mode": "top",
                        "limit": 10,
                    }
                )
            value = projection.query(
                {
                    "month": month,
                    "lineage": journal.lineage,
                    "minimum": status["position"],
                    "epoch": status["epoch"],
                    "mode": "top",
                    "limit": 10,
                }
            )
            self.assertEqual(value["rows"][0]["score"], 1)
