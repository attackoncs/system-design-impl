import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from mailbox_updates_demo import run


class MailboxNetworkTests(unittest.TestCase):
    def test_notifications_folders_read_barrier_and_resumed_replica_deletion(self):
        result = run()
        self.assertEqual(result["resumed_versions"], [2, 3])
        self.assertEqual(result["outsider_events"], 0)
        self.assertTrue(result["same_send_id"])
