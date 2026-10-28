"""Independent-process localhost lab, with isolated SQLite files and real HTTP."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen


def request(base, path, method="GET", body=None, token=None, key=None, region=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    if region:
        headers["X-Region"] = region
    data = (
        json.dumps(body).encode()
        if body is not None
        else (b"" if method == "POST" else None)
    )
    with urlopen(
        Request(base + path, data=data, method=method, headers=headers), timeout=5
    ) as response:
        return json.load(response)


class Cluster:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proximity-")
        self.root = Path(self.temp.name)
        self.processes = {}
        self.urls = {}
        self.tokens = {
            "owners": {"alice-demo": "alice", "bob-demo": "bob"},
            "admin": "admin-demo",
            "replication": "replica-demo",
        }
        (self.root / "tokens.json").write_text(
            json.dumps(self.tokens), encoding="utf-8"
        )

    def start(self, name, role, extra=()):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        args = [
            sys.executable,
            "-m",
            "proximity.cli",
            role,
            "--port",
            str(port),
            "--db",
            str(self.root / (name + ".sqlite")),
            "--tokens",
            str(self.root / "tokens.json"),
            *extra,
        ]
        process = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.processes[name] = process
        self.urls[name] = f"http://127.0.0.1:{port}"
        self.wait(lambda: request(self.urls[name], "/health"), process=process)
        return self.urls[name]

    def wait(self, predicate, timeout=10, process=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError("service exited during startup")
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise RuntimeError("condition timed out")

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for name in list(self.processes):
            self.stop(name)
        self.temp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
