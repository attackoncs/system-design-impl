"""Own and clean up a real six-process localhost lab; never kills foreign processes."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ad_click.common import http_json


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(probe, timeout=15):
    deadline = time.monotonic() + timeout
    last = None
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
    def __init__(self, partitions=4):
        self.root = ROOT / ".runtime" / ("lab-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        names = ["ingress", "api"] + [f"worker-{p}" for p in range(partitions)]
        self.ports = {}
        for name in names:
            value = port()
            while value in self.ports.values():
                value = port()
            self.ports[name] = value
        self.urls = {name: f"http://127.0.0.1:{value}" for name, value in self.ports.items()}
        self.config = {role + "_token": secrets.token_hex(24) for role in ["write", "read", "internal", "admin"]}
        self.config.update(partitions=partitions, ingress_url=self.urls["ingress"],
                           worker_urls=[self.urls[f"worker-{p}"] for p in range(partitions)], lateness=120)
        self.config_path = self.root / "secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.processes, self.logs = {}, []

    def spawn(self, name):
        args = [sys.executable, "-m", "ad_click.cli", name.split("-")[0], "--config", str(self.config_path),
                "--root", str(self.root), "--port", str(self.ports[name])]
        if name.startswith("worker-"):
            args += ["--partition", name.split("-")[1]]
        handle = (self.root / (name + ".log")).open("ab")
        self.logs.append(handle)
        self.processes[name] = subprocess.Popen(args, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
                                                stdout=handle, stderr=handle,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        wait_for(lambda: self.call(name, "/health")["ready"])

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process:
            process.kill()
            process.wait(timeout=10)

    def call(self, name, path, body=None, role="internal"):
        return http_json(self.urls[name] + path, self.config[role + "_token"], body)

    def send(self, events):
        return self.call("ingress", "/events", {"events": events}, "write")

    def query(self, body):
        return self.call("api", "/query", body, "read")

    def caught_up(self):
        heads = self.call("ingress", "/internal/status")["heads"]
        states = {p: self.call(f"worker-{p}", "/internal/status") for p in heads}
        return all(states[p]["seq"] >= head and states[p]["error"] is None for p, head in heads.items())

    def __enter__(self):
        try:
            self.spawn("ingress")
            for p in range(self.config["partitions"]):
                self.spawn(f"worker-{p}")
            self.spawn("api")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        for name in list(self.processes):
            self.stop(name)
        for handle in self.logs:
            handle.close()
