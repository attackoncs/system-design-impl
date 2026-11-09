import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from object_storage.common import request, sha


class PortLost(RuntimeError):
    """A spawned role exited before serving, usually a lost port race."""


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(probe, timeout=15):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"condition not reached: {last}")


class Lab:
    def __init__(self):
        self.root = ROOT / ".runtime" / ("lab-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.config = {
            k: secrets.token_hex(24)
            for k in ("internal_token", "admin_token", "continuation_secret")
        }
        self.config.update(
            users={u: secrets.token_hex(24) for u in ("alice", "bob")},
            write_quorum=2,
        )
        self.config_path = self.root / "secrets.json"
        self.assign_ports()
        self.processes, self.logs = {}, []

    def assign_ports(self):
        """Pick free ports; another process may still take one before bind."""
        self.ports = {}
        for name in ("api", "metadata-0", "metadata-1", "data-0", "data-1", "data-2"):
            value = port()
            while value in self.ports.values():
                value = port()
            self.ports[name] = value
        self.urls = {name: f"http://127.0.0.1:{p}" for name, p in self.ports.items()}
        self.config.update(
            data_urls=[self.urls[f"data-{i}"] for i in range(3)],
            metadata_urls=[self.urls[f"metadata-{i}"] for i in range(2)],
        )
        self.config_path.write_text(json.dumps(self.config))

    def spawn(self, name):
        role = name.split("-")[0]
        args = [
            sys.executable,
            "-m",
            "object_storage.cli",
            role,
            "--config",
            str(self.config_path),
            "--root",
            str(self.root),
            "--port",
            str(self.ports[name]),
        ]
        if role != "api":
            args += ["--index", name.split("-")[1]]
        handle = (self.root / (name + ".log")).open("ab")
        self.logs.append(handle)
        self.processes[name] = subprocess.Popen(
            args,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
            stdout=handle,
            stderr=handle,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        process = self.processes[name]
        deadline, last = time.monotonic() + 15, None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                # Typically the port was taken between port() and bind.
                raise PortLost(f"{name} exited with {process.returncode}")
            try:
                if self.owned(name):
                    return
            except Exception as exc:
                last = exc
            time.sleep(0.05)
        raise AssertionError(f"condition not reached: {last}")

    def owned(self, name):
        """Probe with this lab's secrets: /health alone may reach a stranger."""
        role = name.split("-")[0]
        if role == "data":
            return self.call(name, "/status", role="internal")
        if role == "api":
            return self.call(name, "/v1/buckets") is not None
        try:
            self.call(name, "/lab-probe", role="internal")
        except HTTPError as exc:
            exc.close()
            return exc.code == 404  # authenticated, then unknown route
        return False

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process:
            process.kill()
            process.wait(timeout=10)

    def call(
        self, name, path, body=None, method=None, role="alice", raw=False, headers=None
    ):
        token = (
            self.config["users"][role]
            if role in self.config["users"]
            else self.config[role + "_token"]
        )
        return request(self.urls[name] + path, token, body, method, raw, headers)

    def api(self, path, body=None, method=None, role="alice", raw=False, headers=None):
        return self.call("api", path, body, method, role, raw, headers)

    def put(self, key, data, request_key=None, bucket="bucket-test"):
        return self.api(
            f"/v1/buckets/{bucket}/object?" + urlencode({"key": key}),
            data,
            "PUT",
            headers={
                "X-SHA256": sha(data),
                "Idempotency-Key": request_key or uuid.uuid4().hex,
            },
        )

    def get(self, key, version=None, bucket="bucket-test"):
        args = {"key": key}
        if version:
            args["version"] = version
        return self.api(f"/v1/buckets/{bucket}/object?" + urlencode(args), raw=True)

    def __enter__(self):
        try:
            for attempt in range(3):
                try:
                    for name in (
                        "data-0",
                        "data-1",
                        "data-2",
                        "metadata-0",
                        "metadata-1",
                        "api",
                    ):
                        self.spawn(name)
                    break
                except PortLost:
                    if attempt == 2:
                        raise
                    for name in list(self.processes):
                        self.stop(name)
                    self.assign_ports()
            self.api("/v1/buckets/bucket-test", b"", "PUT")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        for name in list(self.processes):
            self.stop(name)
        for handle in self.logs:
            handle.close()
