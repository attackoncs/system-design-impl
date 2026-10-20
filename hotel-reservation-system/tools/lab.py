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
sys.path.insert(0, str(ROOT/"src"))
from hotel_reservation.common import http_json, shard_for


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(probe, timeout=20):
    end, last = time.monotonic()+timeout, None
    while time.monotonic()<end:
        try:
            result = probe()
            if result:
                return result
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"condition not reached: {last}")


class Lab:
    def __init__(self, hold_seconds=120):
        self.root = ROOT/".runtime"/("lab-"+uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.ports = {}
        for name in ("gateway", "payment", "shard-0", "shard-1"):
            value = port()
            while value in self.ports.values():
                value = port()
            self.ports[name] = value
        self.urls = {name: f"http://127.0.0.1:{p}" for name, p in self.ports.items()}
        self.config = {r+"_token": secrets.token_hex(24) for r in ("admin", "internal", "payment")}
        self.config.update(users={u: secrets.token_hex(24) for u in ("alice", "bob")},
                           shard_urls=[self.urls["shard-0"], self.urls["shard-1"]], payment_url=self.urls["payment"],
                           hold_seconds=hold_seconds, lease_seconds=2)
        self.config_path = self.root/"secrets.json"
        self.config_path.write_text(json.dumps(self.config))
        self.processes, self.logs = {}, []

    def spawn(self, name):
        role = name.split("-")[0]
        args = [sys.executable, "-m", "hotel_reservation.cli", role, "--config", str(self.config_path), "--root", str(self.root)]
        if role in ("worker", "shard"):
            args += ["--shard", name.split("-")[1]]
        if role != "worker":
            args += ["--port", str(self.ports[name])]
        output = (self.root/(name+".log")).open("ab")
        self.logs.append(output)
        self.processes[name] = subprocess.Popen(args, env=dict(os.environ, PYTHONPATH=str(ROOT/"src")),
                                                stdout=output, stderr=output,
                                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if role != "worker":
            wait_for(lambda: self.call(name, "/health"))

    def stop(self, name):
        process = self.processes.pop(name, None)
        if process:
            process.kill()
            process.wait(timeout=10)

    def call(self, name, path, body=None, role="alice", method=None):
        token = self.config["users"][role] if role in self.config["users"] else self.config[role+"_token"]
        return http_json(self.urls[name]+path, token, body, method)

    def api(self, path, body=None, role="alice", method=None):
        return self.call("gateway", path, body, role, method)

    def hotels(self):
        return [next(f"hotel-{i}" for i in range(1000) if shard_for(f"hotel-{i}", 2)==p) for p in range(2)]

    def setup_hotel(self, hotel, start, end, capacity=10, price=10000, overbook=10):
        from datetime import date, timedelta
        self.api(f"/v1/hotels/{hotel}", {"name": "Demo Hotel", "address": "Local test address"}, "admin", "PUT")
        self.api(f"/v1/hotels/{hotel}/rooms/king", {"name": "King", "description": "King room type"}, "admin", "PUT")
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        days = [{"day": (first+timedelta(days=i)).isoformat(), "capacity": capacity, "overbook": overbook, "price": price}
                for i in range((last-first).days)]
        self.api(f"/v1/hotels/{hotel}/rooms/king/calendar", {"days": days}, "admin", "PUT")

    def settled(self):
        return all(self.call("shard-"+str(p), "/internal/status", role="internal")["unfinished_jobs"]==0 for p in range(2))

    def __enter__(self):
        try:
            for name in ("payment", "shard-0", "shard-1", "gateway", "worker-0", "worker-1"):
                self.spawn(name)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        for name in list(self.processes):
            self.stop(name)
        for handle in self.logs:
            handle.close()
