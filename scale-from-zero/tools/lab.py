"""Stage-specific real HTTP processes with isolated files and owned cleanup."""

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from scale_from_zero.common import Error, request
from scale_from_zero.store import partition


def configuration(stage="sharded", ports=None, web_nodes=2):
    if type(web_nodes) is not int or not 1 <= web_nodes <= 8:
        raise ValueError("1..8 Web nodes required")
    if stage not in ("single", "balanced", "sharded"):
        raise ValueError("unknown stage")
    count = {"single": 0, "balanced": 1, "sharded": 2}[stage]
    nodes = (
        ["single"]
        if stage == "single"
        else [
            node + str(index)
            for index in range(count)
            for node in ("primary", "replica")
        ]
        + ["cache"]
        + ["web" + str(i) for i in range(web_nodes)]
        + ["lb", "worker", "edge"]
    )
    ports = list(ports) if ports is not None else range(9900, 9900 + len(nodes))
    if len(ports) != len(nodes):
        raise ValueError("port count mismatch")
    services = {
        node: "http://127.0.0.1:" + str(port) for node, port in zip(nodes, ports)
    }
    users = {}
    for prefix, index in (("alice", 0), ("bob", 1)):
        name = next(
            prefix + "_" + str(i)
            for i in range(100)
            if partition(prefix + "_" + str(i), 2) == index
        )
        users[name] = secrets.token_hex(24)
    return {
        "stage": stage,
        "services": services,
        "entry": services["single"] if stage == "single" else services["lb"],
        "webs": [services["single"]]
        if stage == "single"
        else [services["web" + str(i)] for i in range(web_nodes)],
        "primaries": [services["primary" + str(i)] for i in range(count)],
        "replicas": [services["replica" + str(i)] for i in range(count)],
        "cache": services.get("cache"),
        "edge": services.get("edge"),
        "admin_token": secrets.token_hex(24),
        "internal_token": secrets.token_hex(24),
        "users": users,
    }


def wait(predicate, timeout=20):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = predicate()
            if last:
                return last
        except (Error, OSError, ValueError):
            pass
        time.sleep(0.04)
    raise AssertionError("condition did not become true; last=" + repr(last))


class Lab:
    def __init__(self, stage="sharded", web_nodes=2):
        self.stage, self.web_nodes = stage, web_nodes

    def __enter__(self):
        (ROOT / ".runtime" / "tmp").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(
            prefix="scale-", dir=ROOT / ".runtime" / "tmp"
        )
        self.root = Path(self.temp.name)
        count = len(configuration(self.stage, web_nodes=self.web_nodes)["services"])
        sockets = []
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        self.config = configuration(
            self.stage, [sock.getsockname()[1] for sock in sockets], self.web_nodes
        )
        for sock in sockets:
            sock.close()
        self.path = self.root / "private-config.json"
        self.path.write_text(json.dumps(self.config))
        self.processes, self.logs = {}, {}
        self.sessions = {}
        self.user = next(iter(self.config["users"]))
        try:
            for node in self.config["services"]:
                self.start(node)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def start(self, node):
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "TEMP": str(ROOT / ".runtime" / "tmp"),
            "TMP": str(ROOT / ".runtime" / "tmp"),
        }
        log = open(self.root / (node + ".log"), "ab")
        self.logs[node] = log
        self.processes[node] = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scale_from_zero.cli",
                node,
                "--config",
                str(self.path),
                "--root",
                str(self.root),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        wait(
            lambda: request(self.config["services"][node] + "/health", "", timeout=0.5)
        )

    def stop(self, node):
        proc = self.processes.pop(node, None)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
        if node in self.logs:
            self.logs.pop(node).close()

    def __exit__(self, *args):
        for node in reversed(list(self.processes)):
            self.stop(node)
        target = self.root.resolve()
        if not target.is_relative_to(
            (ROOT / ".runtime" / "tmp").resolve()
        ) or not target.name.startswith("scale-"):
            raise RuntimeError("refusing cleanup outside this lab temporary directory")
        # Windows can retain a recently closed file handle briefly. Retry only
        # that transient error, on this lab's own TemporaryDirectory.
        for attempt in range(10):
            try:
                self.temp.cleanup()
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1)

    def call(self, path, body=None, node=None, auth=None, user=None):
        url = self.config["services"][node] if node else self.config["entry"]
        token = self.config.get(
            (auth or "") + "_token", self.sessions.get(user or self.user, "")
        )
        return request(url + path, token, body)

    def login(self, user=None):
        user = user or self.user
        result = self.call(
            "/v1/login", {"user": user, "password": self.config["users"][user]}
        )
        self.sessions[user] = result["session"]
        return result

    def update(self, version=0, key="write", user=None):
        return self.call(
            "/v1/profile",
            {
                "request_id": key,
                "expected_version": version,
                "display_name": "Synthetic User",
                "city": "Test City " + str(version),
            },
            user=user,
        )

    def caught_up(self):
        for index in range(len(self.config["primaries"])):
            tail = self.call(
                "/admin/status", node="primary" + str(index), auth="admin"
            )["seq"]
            wait(
                lambda: (
                    self.call(
                        "/admin/status", node="replica" + str(index), auth="admin"
                    )["seq"]
                    == tail
                )
            )

    def drained(self):
        for index in range(len(self.config["primaries"])):
            wait(
                lambda: (
                    self.call(
                        "/admin/status", node="primary" + str(index), auth="admin"
                    )["pending_jobs"]
                    == 0
                )
            )
