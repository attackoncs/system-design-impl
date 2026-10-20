"""Actual API process and separate filesystem clients; measure delta transfer."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import platform
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from file_sync import Drive
from file_sync.client import DeviceClient


def run_demo(output=None):
    module = Path(__file__).resolve().parents[1]
    runtime = module / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fs-", dir=runtime) as directory:
        root = Path(directory).resolve()
        root.relative_to(runtime.resolve())
        backend = root / "server"
        backend.mkdir()
        drive = Drive(backend / "metadata.sqlite3", backend / "objects", block_size=4096)
        token = drive.issue("demo-owner")
        space = drive.create_space(token, "Demo")
        drive.close()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(module / "src")
        env["FILE_SYNC_TOKEN"] = token
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        args = [sys.executable, "-m", "file_sync.cli", "--root", str(backend), "--block-size", "4096"]
        child = subprocess.Popen(args + ["api", "--port", str(port)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
        client = None
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urlopen(Request(url + "/config", headers={"Authorization": "Bearer " + token}), timeout=.5):
                        break
                except OSError:
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("API failed to start")
                    time.sleep(.05)
            for folder in (root / "a", root / "b"):
                folder.mkdir()
            original = os.urandom(4096) + os.urandom(4096) + os.urandom(4096)
            (root / "a/data.bin").write_bytes(original)
            def device(name, command):
                subprocess.run([sys.executable, "-m", "file_sync.cli", command, "--url", url,
                    "--folder", str(root / name), "--device", name] +
                    (["--space", space, "data.bin"] if command != "pull" else []),
                    env=env, capture_output=True, timeout=15, check=True, **options)
            started = time.perf_counter()
            device("a", "push")
            device("b", "pull")
            initial_ms = (time.perf_counter() - started) * 1000
            assert (root / "b/data.bin").read_bytes() == original
            modified = original[:4096] + os.urandom(4096) + original[8192:]
            (root / "a/data.bin").write_bytes(modified)
            device("a", "push")
            client = DeviceClient(url, token, root / "b", "b")
            started = time.perf_counter()
            client.pull()
            delta_ms = (time.perf_counter() - started) * 1000
            assert (root / "b/data.bin").read_bytes() == modified
            assert client.transferred_down == 4096
            client.close()
            client = None
            # Server restart must retain immutable revisions and device progress.
            child.terminate()
            child.wait(timeout=5)
            child = subprocess.Popen(args + ["api", "--port", str(port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
            deadline = time.monotonic() + 10
            while True:
                try:
                    client = DeviceClient(url, token, root / "b", "b")
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.05)
            assert not client.pull()["events"]
            metadata = Drive(backend / "metadata.sqlite3", backend / "objects", block_size=4096)
            try:
                logical = metadata.db.execute("SELECT SUM(size) FROM revisions").fetchone()[0]
                unique = metadata.db.execute("SELECT SUM(size) FROM blocks").fetchone()[0]
                physical = sum(metadata.store.path(r[0]).stat().st_size for r in metadata.db.execute("SELECT key FROM blocks"))
            finally:
                metadata.close()
            report = {"file_bytes": len(original), "block_bytes": 4096,
                      "initial_upload_then_second_process_pull_ms": round(initial_ms, 3),
                      "delta_pull_ms": round(delta_ms, 3), "delta_download_bytes": 4096,
                      "unchanged_bytes_reused": 8192, "server_restart_cursor_verified": True,
                      "retained_revision_bytes": logical, "unique_plaintext_block_bytes": unique,
                      "encrypted_object_bytes": physical,
                      "environment": {"python": platform.python_version(), "platform": platform.platform()},
                      "scope": "one local API process; independent filesystem client processes; loopback HTTP"}
            if output:
                target = Path(output)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            if client:
                client.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
