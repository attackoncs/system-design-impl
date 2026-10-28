from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

from helpers_review import StoreCase, configuration
from scale_from_zero.app import App
from scale_from_zero.cache import Cache
from scale_from_zero.common import Error


class AppSafetyTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.config = configuration("balanced")
        self.user = next(iter(self.config["users"]))
        self.app = App(self.config, "web0")
        self.cache = Cache()

        def primary(user, action, body):
            return (
                self.store.update(user, body["profile"])
                if action == "update"
                else self.store.get(user)
            )

        def cache(action, body):
            if action == "get":
                return self.cache.probe(body["key"], body["min_version"])
            if action == "delete":
                self.cache.delete(body["key"], body["min_version"])
                return {"deleted": True}
            return {
                "stored": self.cache.put(body["key"], body["value"], body["generation"])
            }

        self.app.primary = primary
        self.app.cache = cache
        self.old = self.store.update(self.user, self.body())

    def test_observed_write_floor_bypasses_stale_replica(self):
        self.app.update(self.user, self.body("next", 1))
        with patch("scale_from_zero.app.request", return_value=self.old):
            value = self.app.get(self.user)
        self.assertEqual((value["version"], value["source"]), (2, "primary"))
        self.assertEqual(self.cache.get(self.user)["version"], 2)

    def test_inflight_read_cannot_repopulate_cache_after_write_invalidation(self):
        entered, release = threading.Event(), threading.Event()

        def replica(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("test never released replica")
            return self.old

        with (
            patch("scale_from_zero.app.request", side_effect=replica),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(self.app.get, self.user)
            try:
                self.assertTrue(entered.wait(1))
                self.app.update(self.user, self.body("next", 1))
            finally:
                release.set()
            self.assertEqual(future.result(2)["version"], 1)
        self.assertIsNone(self.cache.get(self.user))
        with patch("scale_from_zero.app.request", return_value=self.old):
            self.assertEqual(self.app.get(self.user)["version"], 2)

    def test_wrong_user_cache_and_replica_reply_never_leak_profile(self):
        other = {**self.old, "user": "other"}
        self.cache.put(self.user, other)
        with patch("scale_from_zero.app.request", return_value=other):
            value = self.app.get(self.user)
        self.assertEqual(value["user"], self.user)
        self.assertEqual(value["source"], "primary")

    def test_strong_reads_skip_cache_and_replica_and_future_version_is_conflict(self):
        with (
            patch.object(
                self.app, "cache", side_effect=AssertionError("cache consulted")
            ),
            patch(
                "scale_from_zero.app.request",
                side_effect=AssertionError("replica consulted"),
            ),
        ):
            self.assertEqual(self.app.get(self.user, strong=True)["version"], 1)
            with self.assertRaises(Error) as error:
                self.app.get(self.user, strong=True, min_version=2)
        self.assertEqual(error.exception.status, 409)

    def test_invalid_login_unicode_password_and_unknown_session_fail_before_rpc(self):
        with patch.object(
            self.app, "primary", side_effect=AssertionError("unexpected RPC")
        ):
            with self.assertRaises(Error) as error:
                self.app.login({"user": self.user, "password": "\u5bc6" * 24})
            self.assertEqual(error.exception.status, 401)
            with self.assertRaises(Error):
                self.app.authorize("Bearer unknown." + "a" * 48)

    def test_config_is_detached_from_callers(self):
        self.config["internal_token"] = "changed"
        self.assertNotEqual(self.app.config["internal_token"], "changed")

    def test_primary_cannot_return_below_the_cache_observed_write_floor(self):
        self.cache.delete(self.user, 2)
        with patch("scale_from_zero.app.request", return_value=self.old):
            with self.assertRaises(Error):
                self.app.get(self.user)
        self.assertIsNone(self.cache.get(self.user))

    def test_update_reply_must_match_the_submitted_content_and_version(self):
        with (
            patch.object(self.app, "primary", return_value=self.old),
            patch.object(self.app, "cache") as cache,
        ):
            with self.assertRaises(Error) as error:
                self.app.update(self.user, self.body("next", 1))
            self.assertEqual(error.exception.status, 502)
            cache.assert_not_called()
