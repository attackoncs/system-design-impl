from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab
from scale_from_zero.assets import fetch_asset


class FeaturesNetworkTests(unittest.TestCase):
    def test_online_web_pool_cache_fill_fence_and_client_origin_fallback(self):
        with Lab("balanced", web_nodes=3) as lab:
            lab.login()
            lab.update()
            lab.caught_up()
            initial = lab.call("/v1/profile?min_version=1")
            probe = lab.call(
                "/internal/get",
                {"key": lab.user, "min_version": 0},
                node="cache",
                auth="internal",
            )
            lab.update(1, "next")
            stale = {key: initial[key] for key in ("user", "version", "profile")}
            result = lab.call(
                "/internal/put",
                {"key": lab.user, "value": stale, "generation": probe["generation"]},
                node="cache",
                auth="internal",
            )
            self.assertFalse(result["stored"])
            self.assertEqual(lab.call("/v1/profile")["version"], 2)
            lab.call("/admin/backends", {"nodes": ["web0"]}, node="lb", auth="admin")
            self.assertEqual(lab.call("/v1/profile")["web_node"], "web0")
            lab.call(
                "/admin/backends",
                {"nodes": ["web0", "web1", "web2"]},
                node="lb",
                auth="admin",
            )
            self.assertEqual(
                {lab.call("/v1/profile")["web_node"] for _ in range(6)},
                {"web0", "web1", "web2"},
            )
            self.assertEqual(
                fetch_asset(lab.config, "app-v1.txt")["asset_source"], "edge"
            )
            lab.call(
                "/admin/invalidate", {"asset": "app-v1.txt"}, node="edge", auth="admin"
            )
            self.assertEqual(
                lab.call("/admin/status", node="edge", auth="admin")["entries"], 0
            )
            lab.stop("edge")
            self.assertEqual(
                fetch_asset(lab.config, "app-v1.txt")["asset_source"], "origin_fallback"
            )
            lab.stop("web0")
            self.assertNotEqual(lab.call("/v1/profile")["web_node"], "web0")
