from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from scale_from_zero.balancer import LoadBalancer


class BalancerSafetyTests(unittest.TestCase):
    def test_pool_update_does_not_change_inflight_attempt_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        urls = []

        def transport(url, *args, **kwargs):
            urls.append(url)
            if url.startswith("http://old1"):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release I/O")
                raise OSError("old endpoint failed")
            return url

        balancer = LoadBalancer(["http://old1", "http://old2"], transport)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(balancer.forward, "GET", "/v1/profile", "")
            try:
                self.assertTrue(entered.wait(1))
                balancer.configure(["http://new"])
                self.assertEqual(
                    balancer.forward("GET", "/v1/profile", ""), "http://new/v1/profile"
                )
            finally:
                release.set()
            self.assertEqual(first.result(2), "http://old2/v1/profile")
        self.assertEqual(len(urls), 3)

    def test_unsafe_login_does_not_replay_after_pool_replacement(self):
        attempts = []

        def transport(url, *args, **kwargs):
            attempts.append(url)
            raise OSError("reply lost")

        balancer = LoadBalancer(["a", "b"], transport)
        from scale_from_zero.common import Error

        with self.assertRaises(Error):
            balancer.forward("POST", "/v1/login", "", {"request_id": "looks-safe"})
        self.assertEqual(len(attempts), 1)
