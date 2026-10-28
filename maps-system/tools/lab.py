import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from maps_service.store import Store


def request(base, path, method="GET", body=None, token=None, headers=None, raw=False):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(
        Request(base + path, data=data, method=method, headers=headers), timeout=10
    ) as response:
        payload = response.read()
        return (payload, dict(response.headers)) if raw else json.loads(payload)


class Cluster:
    def __init__(self):
        self.root = ROOT / ".runtime" / ("lab-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.processes = {}
        self.urls = {}
        self.logs = []
        store = Store(self.root / "primary.sqlite")
        store.import_graph(json.loads((ROOT / "examples/roads.json").read_text()))
        store.provision("admin", "admin-demo", admin=True)
        store.provision("alice", "alice-demo")
        store.provision("bob", "bob-demo")
        (self.root / "admin.token").write_text("admin-demo")

    def start(self, name, role, extra=()):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        log = (self.root / (name + ".log")).open("wb")
        self.logs.append(log)
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        args = [
            sys.executable,
            "-m",
            "maps_service.cli",
            role,
            "--db",
            str(self.root / (name + ".sqlite")),
            "--port",
            str(port),
            *extra,
        ]
        process = subprocess.Popen(
            args,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.processes[name] = process
        url = f"http://127.0.0.1:{port}"
        self.urls[name] = url
        self.wait(lambda: request(url, "/health"), process=process)
        return url

    def wait(self, predicate, process=None, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError("service exited; inspect isolated lab log")
            try:
                result = predicate()
                if result:
                    return result
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
        for log in self.logs:
            log.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def start_all(self):
        primary = self.start("primary", "serve")
        replica = self.start(
            "replica",
            "replica",
            [
                "--upstream",
                primary,
                "--token-file",
                str(self.root / "admin.token"),
                "--interval",
                "0.1",
            ],
        )
        self.wait(lambda: request(replica, "/health")["ready"])
        regionfile = self.root / "regions.json"
        regionfile.write_text(
            json.dumps(
                {"local": [primary, replica], "isolated": ["http://127.0.0.1:1"]}
            )
        )
        gateway = self.start("gateway", "gateway", ["--regions", str(regionfile)])
        return primary, replica, gateway
