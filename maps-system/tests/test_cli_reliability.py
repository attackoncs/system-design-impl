"""Provisioning failure boundaries and owned CLI helper lifetimes."""

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from maps_service.cli import main
from maps_service.credentials import provision_token
from maps_service.maintenance import Maintenance
from maps_service.store import Store


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        self.path = self.root / "alice.token"

    def test_persisted_credential_authenticates_after_restart(self):
        provision_token(self.store, "alice", False, self.path)
        reopened = Store(self.root / "state.sqlite")
        self.assertEqual(reopened.authenticate(self.path.read_text()), "alice")

    def test_existing_token_file_is_preserved_without_creating_user(self):
        self.path.write_text("existing", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            provision_token(self.store, "alice", False, self.path)
        self.assertEqual(self.path.read_text(), "existing")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_failed_database_insert_removes_only_new_token_file(self):
        self.store.provision("alice", "existing-private-token")
        with self.assertRaises(sqlite3.IntegrityError):
            provision_token(self.store, "alice", False, self.path)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.authenticate("existing-private-token"), "alice")

    def test_failed_file_sync_does_not_create_database_user(self):
        with patch(
            "maps_service.credentials.os.fsync", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                provision_token(self.store, "alice", False, self.path)
        self.assertFalse(self.path.exists())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 0)

    def test_bind_failure_starts_no_background_helpers(self):
        with (
            patch("maps_service.cli.server", side_effect=OSError("bind failed")),
            patch("maps_service.cli.NavigationTracker.start") as navigation,
            patch("maps_service.cli.Maintenance.start") as maintenance,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(OSError):
                main(["serve", "--db", str(self.root / "state.sqlite")])
        navigation.assert_not_called()
        maintenance.assert_not_called()

    def test_serve_rejects_private_catalog_as_public_tile_archive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(
                [
                    "serve",
                    "--db",
                    str(self.root / "state.sqlite"),
                    "--tile-archive",
                    str(self.root / "state.sqlite"),
                ]
            )
        with self.store.transaction() as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='tile_objects'"
                ).fetchone()
            )


class MaintenanceTests(unittest.TestCase):
    def test_close_waits_for_accepted_io_and_prevents_restart(self):
        entered, release = threading.Event(), threading.Event()

        def action():
            entered.set()
            self.assertTrue(release.wait(3))

        worker = Maintenance(action, interval=0.005).start()
        try:
            self.assertTrue(entered.wait(2))
            with ThreadPoolExecutor(1) as pool:
                closing = pool.submit(worker.close)
                self.assertFalse(closing.done())
                release.set()
                closing.result(2)
        finally:
            release.set()
            worker.close()
        self.assertFalse(worker.thread.is_alive())
        with self.assertRaises(RuntimeError):
            worker.start()

    def test_failure_retries_without_exposing_action_error(self):
        called, recovered = [], threading.Event()

        def action():
            called.append(1)
            if len(called) == 1:
                raise OSError("private action context")
            recovered.set()

        worker = Maintenance(action, interval=0.005)
        with self.assertLogs("maps_service.maintenance", level="WARNING") as logs:
            worker.start()
            try:
                self.assertTrue(recovered.wait(2))
            finally:
                worker.close()
        self.assertIsNone(worker.last_error)
        self.assertNotIn("private action context", " ".join(logs.output))

    def test_configuration_rejects_unbounded_and_boolean_intervals(self):
        for value in (True, 0, -1, "30", float("nan"), float("inf"), 10**1000):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaises(ValueError),
            ):
                Maintenance(lambda: None, value)


if __name__ == "__main__":
    unittest.main()
