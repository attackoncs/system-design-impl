from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_journal import stamp
from gaming_leaderboard.common import Error, lex_member, shard_for
from gaming_leaderboard.journal import Journal
from gaming_leaderboard.profiles import ProfileCache
from gaming_leaderboard.service import Service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = stamp("2030-01-01T12:00:00")
        self.journal = Journal(
            Path(self.temp.name) / "source.sqlite", clock=lambda: self.now
        )
        self.users = [f"user{i:02}" for i in range(14)]
        for user in self.users:
            self.journal.register(user, user)
        self.config = {
            "projection_urls": ["http://n0", "http://n1"],
            "internal_token": "token",
        }
        self.service = Service(self.config, self.journal)
        self.addCleanup(self.service.close)

    def rpc(self, url, token, body=None, **kwargs):
        shard = int(url.split("/")[2][1:])
        envelope = {
            "shard": shard,
            "position": self.journal.status()["heads"][str(shard)],
            "lineage": self.journal.lineage,
            "epoch": "epoch" + str(shard),
        }
        if url.endswith("status"):
            return envelope
        with self.journal.transaction() as db:
            values = [
                {
                    "user": row["user"],
                    "score": row["score"],
                    "lex": lex_member(row["user"], row["score"]),
                }
                for row in db.execute(
                    "SELECT user,score FROM scores WHERE month=?", (body["month"],)
                )
                if shard_for(row["user"], 2) == shard
            ]
        values.sort(key=lambda value: value["lex"])
        mode = body["mode"]
        if mode in ("top", "dump"):
            return {**envelope, "rows": values[:10] if mode == "top" else values}
        if mode == "target":
            target = next(
                (value for value in values if value["user"] == body["user"]), None
            )
            return {**envelope, **({"target": target} if target else {})}
        if mode == "around":
            before = [value for value in values if value["lex"] < body["pivot"]]
            after = [value for value in values if value["lex"] > body["pivot"]]
            return {
                **envelope,
                "before": len(before),
                "higher": sum(value["score"] > body["score"] for value in values),
                "above": list(reversed(before))[: body["radius"]],
                "below": after[: body["radius"]],
            }
        return {
            **envelope,
            "counts": {
                str(score): sum(value["score"] > score for value in values)
                for score in body["scores"]
            },
        }

    def win(self, user, match):
        return self.journal.ingest(
            {
                "match_id": match,
                "winner": user,
                "players": [user, self.users[-1]],
                "ended_at": self.now,
            }
        )

    def test_exact_ties_and_global_neighbor_positions(self):
        for user, wins in zip(self.users[:4], [3, 2, 2, 1]):
            for i in range(wins):
                self.win(user, user + str(i))
        with patch("gaming_leaderboard.service.http_json", side_effect=self.rpc):
            self.assertEqual(
                [row["rank"] for row in self.service.query()["rows"]],
                [1, 2, 2, 4, 5, 5, 5, 5, 5, 5],
            )
            result = self.service.query(user=self.users[7], radius=4)["rows"]
            self.assertEqual([row["position"] for row in result], list(range(4, 13)))

    def test_profile_revision_invalidates_cached_names(self):
        with patch("gaming_leaderboard.service.http_json", side_effect=self.rpc):
            self.service.query()
            self.service.query()
            self.assertGreater(self.service.metrics()["profile_cache"]["hits"], 0)
            self.journal.register(self.users[0], "Renamed")
            self.assertEqual(self.service.query()["rows"][0]["name"], "Renamed")

    def test_readers_overlap_and_writer_waits_for_complete_snapshot(self):
        entered, release = threading.Barrier(3), threading.Event()

        def rpc(url, *args, **kwargs):
            body = args[1] if len(args) > 1 else kwargs.get("body")
            if body and body["mode"] == "top" and url.startswith("http://n0"):
                entered.wait(timeout=2)
                release.wait(timeout=2)
            return self.rpc(url, *args, **kwargs)

        def write():
            with (
                self.service.operation(deadline=time.monotonic() + 3),
                self.service.guard("POST", "/v1/scores"),
            ):
                self.win(self.users[0], "win")

        with (
            patch("gaming_leaderboard.service.http_json", side_effect=rpc),
            ThreadPoolExecutor(max_workers=3) as pool,
        ):
            first, second = (
                pool.submit(self.service.query),
                pool.submit(self.service.query),
            )
            entered.wait(timeout=2)
            writer = pool.submit(write)
            self.assertFalse(writer.done())
            release.set()
            self.assertEqual(first.result(timeout=3)["rows"][0]["score"], 0)
            self.assertEqual(second.result(timeout=3)["rows"][0]["score"], 0)
            writer.result(timeout=3)

    def test_epoch_change_between_rank_phases_fails_closed(self):
        def rpc(url, token, body=None, **kwargs):
            value = self.rpc(url, token, body, **kwargs)
            if body and body["mode"] == "counts":
                value["epoch"] = "reset"
            return value

        with (
            patch("gaming_leaderboard.service.http_json", side_effect=rpc),
            self.assertRaises(Error) as caught,
        ):
            self.service.query(user=self.users[4], radius=4)
        self.assertEqual(caught.exception.status, 503)

    def test_future_projection_head_cannot_mix_with_source_snapshot(self):
        def rpc(url, token, body=None, **kwargs):
            value = self.rpc(url, token, body, **kwargs)
            if body:
                value["position"] += 1
            return value

        with (
            patch("gaming_leaderboard.service.http_json", side_effect=rpc),
            self.assertRaises(Error) as caught,
        ):
            self.service.query()
        self.assertEqual(caught.exception.status, 503)

    def test_malformed_projection_candidate_is_rejected(self):
        def rpc(url, token, body=None, **kwargs):
            value = self.rpc(url, token, body, **kwargs)
            if body and body["mode"] == "top" and value["rows"]:
                value["rows"][0]["score"] = True
            return value

        with (
            patch("gaming_leaderboard.service.http_json", side_effect=rpc),
            self.assertRaises(Error) as caught,
        ):
            self.service.query()
        self.assertEqual(caught.exception.status, 502)

    def test_new_month_prepares_every_registered_zero_member(self):
        self.now = stamp("2030-02-01T00:00:00")
        with patch("gaming_leaderboard.service.http_json", side_effect=self.rpc):
            result = self.service.query()
            self.assertEqual(result["month"], "2030-02")
            self.assertTrue(all(row["score"] == 0 for row in result["rows"]))
            self.assertEqual(len(self.journal.rows("2030-02")), 14)

    def test_reconcile_detects_consistently_wrong_scores(self):
        with patch("gaming_leaderboard.service.http_json", side_effect=self.rpc):
            self.assertTrue(self.service.reconcile("2030-01")["matches"])

    def test_profile_cache_stays_bounded_and_returns_fresh_dicts(self):
        cache = ProfileCache(capacity=2, byte_limit=100)
        first = cache.names(self.journal, 1, self.users)
        first.clear()
        self.assertEqual(
            cache.names(self.journal, 1, [self.users[0]]),
            {self.users[0]: self.users[0]},
        )
        self.assertLessEqual(cache.stats()["entries"], 2)
        self.assertLessEqual(cache.stats()["bytes"], 100)

    def test_bad_radius_and_stopped_service_are_rejected(self):
        with self.assertRaises(ValueError):
            self.service.query(radius=True)
        self.service.close()
        with self.assertRaises(Error):
            self.service.query()
