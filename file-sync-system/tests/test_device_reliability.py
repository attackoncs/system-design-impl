"""Data preservation, response ownership, local state and automatic sync."""

import contextlib
import http.server
import json
import threading
import time
from urllib.error import HTTPError

import pytest

from file_sync.client import DeviceClient
from file_sync.watcher import SyncWatcher
from test_sync import drive as drive, network, upload


def test_folder_is_owned_until_close_and_constructor_failure_releases_it(
    drive, tmp_path, monkeypatch
):
    folder = tmp_path / "device"
    with network(drive) as url:
        first = DeviceClient(url, drive.token, folder, "one")
        try:
            with pytest.raises(RuntimeError, match="in use"):
                DeviceClient(url, drive.token, folder, "one")
        finally:
            first.close()
        original = DeviceClient.request
        monkeypatch.setattr(
            DeviceClient,
            "request",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
        )
        with pytest.raises(OSError):
            DeviceClient(url, drive.token, folder, "one")
        monkeypatch.setattr(DeviceClient, "request", original)
        reopened = DeviceClient(url, drive.token, folder, "one")
        reopened.close()


def test_push_records_accepted_revision_instead_of_concurrent_new_head(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"abcdefgh")
            original = client.request
            newer = []

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route == "/commit":
                    newer.append(
                        upload(
                            drive,
                            drive.token,
                            drive.space,
                            b"ijklmnop",
                            "a",
                            response["revision"],
                        )
                    )
                return response

            monkeypatch.setattr(client, "request", request)
            accepted = client.push(drive.space, "a")
            assert (
                client.db.execute("SELECT revision FROM files").fetchone()[0]
                == accepted["revision"]
            )
            client.pull()
            assert (client.root / "a").read_bytes() == b"ijklmnop"
            assert (
                client.db.execute("SELECT revision FROM files").fetchone()[0]
                == newer[0]["revision"]
            )
        finally:
            client.close()


def test_edit_arriving_during_download_is_preserved(drive, tmp_path, monkeypatch):
    first = upload(drive, drive.token, drive.space, b"abcdefgh", "a")
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            client.pull()
            upload(drive, drive.token, drive.space, b"ijklmnop", "a", first["revision"])
            original, edited = client.request, []

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route.startswith("/block?") and not edited:
                    (client.root / "a").write_bytes(b"late local edit")
                    edited.append(True)
                return response

            monkeypatch.setattr(client, "request", request)
            client.pull()
            assert (client.root / "a").read_bytes() == b"ijklmnop"
            assert (
                next(client.root.glob("a.local-conflict-*")).read_bytes()
                == b"late local edit"
            )
        finally:
            client.close()


def test_lost_commit_then_local_edit_reconciles_previous_base(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"abcdefgh")
            original, lost = client.request, []

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route == "/commit" and not lost:
                    lost.append(True)
                    raise OSError("response lost")
                return response

            monkeypatch.setattr(client, "request", request)
            with pytest.raises(OSError):
                client.push(drive.space, "a")
            (client.root / "a").write_bytes(b"ijklmnop")
            result = client.push(drive.space, "a")
            assert not result["conflict"]
            assert len(drive.list_files(drive.token, drive.space)) == 1
            assert len(drive.revisions(drive.token, result["file"])) == 2
        finally:
            client.close()


def test_reverting_interrupted_upload_releases_reservation(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            path = client.root / "a"
            path.write_bytes(b"abcdefgh")
            client.push(drive.space, "a")
            path.write_bytes(b"ijklmnop")
            original = client.request

            def unavailable(method, route, value=None, binary=False):
                if method == "PUT":
                    raise OSError("offline")
                return original(method, route, value, binary)

            monkeypatch.setattr(client, "request", unavailable)
            with pytest.raises(OSError):
                client.push(drive.space, "a")
            assert drive.quota("alice")["reserved"] == 8
            path.write_bytes(b"abcdefgh")
            assert client.push(drive.space, "a") == {"unchanged": True}
            assert drive.quota("alice")["reserved"] == 0
        finally:
            client.close()


def test_lost_delete_response_reconciles_tombstone_and_preserves_new_edit(
    drive, tmp_path, monkeypatch
):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            (client.root / "a").write_bytes(b"abcdefgh")
            client.push(drive.space, "a")
            original, lost = client.request, []

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route == "/delete" and not lost:
                    lost.append(True)
                    raise OSError("response lost")
                return response

            monkeypatch.setattr(client, "request", request)
            with pytest.raises(OSError):
                client.delete(drive.space, "a")
            (client.root / "a").write_bytes(b"new edit after delete")
            client.delete(drive.space, "a")
            assert not (client.root / "a").exists()
            assert (
                next(client.root.glob("a.local-conflict-*")).read_bytes()
                == b"new edit after delete"
            )
            assert client.db.execute("SELECT deleted FROM files").fetchone()[0] == 1
        finally:
            client.close()


def test_cache_is_bounded_and_corrupt_entries_are_not_trusted(drive, tmp_path):
    uploaded = upload(drive, drive.token, drive.space, b"abcdefghijkl", "a")
    with network(drive) as url:
        client = DeviceClient(
            url, drive.token, tmp_path / "device", "one", cache_bytes=4
        )
        try:
            client.pull()
            assert sum(path.stat().st_size for path in client.cache.iterdir()) <= 4
            assert (client.root / "a").read_bytes() == b"abcdefghijkl"
            cached = next(client.cache.iterdir())
            cached.write_bytes(b"bad!")
            upload(
                drive,
                drive.token,
                drive.space,
                b"xxxxxxxijklm",
                "a",
                uploaded["revision"],
            )
            client.pull()
            assert (client.root / "a").read_bytes() == b"xxxxxxxijklm"
            assert sum(path.stat().st_size for path in client.cache.iterdir()) <= 4
        finally:
            client.close()


def test_bad_server_manifest_does_not_escape_cache_or_advance_cursor(
    drive, tmp_path, monkeypatch
):
    upload(drive, drive.token, drive.space, b"abcd", "a")
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "device", "one")
        try:
            original = client.request

            def request(method, route, value=None, binary=False):
                response = original(method, route, value, binary)
                if route.startswith("/metadata?"):
                    response["blocks"][0]["digest"] = "../../escape"
                return response

            monkeypatch.setattr(client, "request", request)
            with pytest.raises(OSError):
                client.pull()
            assert (
                client.db.execute(
                    "SELECT value FROM state WHERE key='cursor'"
                ).fetchone()[0]
                == "0"
            )
            assert not (client.root / "a").exists()
        finally:
            client.close()


@contextlib.contextmanager
def serving(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_api_redirect_never_forwards_bearer_token(tmp_path):
    visited = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            visited.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/credential-target")
            self.send_header("Content-Length", "0")
            self.end_headers()

    with serving(Handler) as url:
        with pytest.raises(HTTPError) as error:
            DeviceClient(url, "private-token", tmp_path / "device", "one")
        assert error.value.code == 302
    assert visited == ["/config"]


def test_slow_body_cannot_reset_request_budget(tmp_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            value = json.dumps({"block_size": 4}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            try:
                for char in value:
                    self.wfile.write(bytes([char]))
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

    with serving(Handler) as url:
        before = time.monotonic()
        with pytest.raises((OSError, ValueError)):
            DeviceClient(url, "private-token", tmp_path / "device", "one", timeout=0.1)
        assert time.monotonic() - before < 1


def test_watcher_automatically_adds_edits_and_deletes(drive, tmp_path):
    with network(drive) as url:
        a = DeviceClient(url, drive.token, tmp_path / "a", "a")
        b = DeviceClient(url, drive.token, tmp_path / "b", "b")
        try:
            wa, wb = (
                SyncWatcher(client, drive.space, poll_wait=0.001) for client in (a, b)
            )
            (a.root / "a").write_bytes(b"abcdefgh")
            wa.run_once()
            wb.run_once()
            assert (b.root / "a").read_bytes() == b"abcdefgh"
            (b.root / "a").write_bytes(b"abcdijkl")
            wb.run_once()
            wa.run_once()
            assert (a.root / "a").read_bytes() == b"abcdijkl"
            # An offline device retains its applied cursor and workspace binding.
            b.close()
            (a.root / "a").write_bytes(b"mnopqrst")
            wa.run_once()
            b = DeviceClient(url, drive.token, tmp_path / "b", "b")
            wb = SyncWatcher(b, drive.space, poll_wait=0.001)
            wb.run_once()
            assert (b.root / "a").read_bytes() == b"mnopqrst"
            (a.root / "a").unlink()
            wa.run_once()
            wb.run_once()
            assert not (b.root / "a").exists()
            assert wb.run_once()["deleted"] == 0
        finally:
            a.close()
            b.close()


def test_bound_workspace_ignores_overlapping_other_workspace_events(drive, tmp_path):
    upload(drive, drive.token, drive.space, b"aaaa", "a")
    other = drive.create_space(drive.token, "Other")
    upload(drive, drive.token, other, b"bbbb", "a")
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "one", "one")
        try:
            watcher = SyncWatcher(client, drive.space, poll_wait=0.001)
            watcher.run_once()
            assert (client.root / "a").read_bytes() == b"aaaa"
            with pytest.raises(ValueError):
                client.bind_space(other)
            assert {row[0] for row in client.db.execute("SELECT space FROM files")} == {
                drive.space
            }
        finally:
            client.close()
        reopened = DeviceClient(url, drive.token, tmp_path / "one", "one")
        try:
            assert reopened.space == drive.space
        finally:
            reopened.close()


def test_watcher_scan_is_bounded_before_any_upload(drive, tmp_path):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "one", "one")
        try:
            (client.root / "a").write_bytes(b"a")
            (client.root / "b").write_bytes(b"b")
            watcher = SyncWatcher(client, drive.space, max_files=1, poll_wait=0.001)
            with pytest.raises(OSError, match="capacity"):
                watcher.run_once()
            assert drive.list_files(drive.token, drive.space) == []
        finally:
            client.close()


def test_watcher_retries_with_interruptible_stop(drive, tmp_path, monkeypatch):
    with network(drive) as url:
        client = DeviceClient(url, drive.token, tmp_path / "one", "one")
        try:
            watcher = SyncWatcher(client, drive.space, interval=0.001, poll_wait=0.001)
            stop, calls, failures = threading.Event(), [], []

            def cycle(_stop):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("offline")
                stop.set()

            monkeypatch.setattr(watcher, "run_once", cycle)
            watcher.run(stop, on_error=failures.append)
            assert len(calls) == 2 and len(failures) == 1
        finally:
            client.close()
