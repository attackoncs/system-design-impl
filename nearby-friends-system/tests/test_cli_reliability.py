"""Credential provisioning keeps file and local account failures recoverable."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nearby_friends.cli import provision_token
from nearby_friends.core import Error, Store


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        self.path = self.root / "alice.token"

    def test_written_credential_authenticates_after_reopening_database(self):
        provision_token(self.store, "alice", False, self.path)
        reopened = Store(self.root / "state.sqlite")
        self.assertEqual(reopened.authenticate(self.path.read_text()), "alice")

    def test_existing_file_is_preserved_and_no_account_is_created(self):
        self.path.write_text("existing-credential", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            provision_token(self.store, "alice", False, self.path)
        self.assertEqual(self.path.read_text(), "existing-credential")
        with self.assertRaises(Error):
            self.store.authenticate("existing-credential")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_database_rejection_removes_only_new_file_and_preserves_account(self):
        self.store.provision("alice", "original-private-token")
        with self.assertRaises(sqlite3.IntegrityError):
            provision_token(self.store, "alice", False, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.authenticate("original-private-token"), "alice")

    def test_file_sync_failure_does_not_leave_an_account_or_empty_credential(self):
        with patch("nearby_friends.cli.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                provision_token(self.store, "alice", False, self.path)
        self.assertFalse(self.path.exists())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
