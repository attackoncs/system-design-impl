import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from listing_demo import run


class ListingNetworkTests(unittest.TestCase):
    def test_private_buckets_frozen_delimiter_and_concurrent_multipart(self):
        result = run()
        self.assertEqual(result["snapshot_common_prefixes"], ["CA/", "NY/"])
        self.assertTrue(result["concurrent_completions_same_version"])
        self.assertEqual(result["other_owner_buckets"], 0)
