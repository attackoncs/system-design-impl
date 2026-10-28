from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import configuration as configuration
from scale_from_zero.store import Store, Consumer


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.store = Store(self.root / "primary.sqlite", lambda: self.now)
        self.replica = Store(self.root / "replica.sqlite", lambda: self.now)
        self.consumer = Consumer(self.root / "consumer.sqlite")

    def body(self, name="write", version=0):
        return {
            "request_id": name,
            "expected_version": version,
            "display_name": "Synthetic User",
            "city": "Test",
        }
