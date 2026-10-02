"""Temporary Linux 3-node PostgreSQL/3-member etcd lab, controlled by local files.

Run with the Python written to .runtime/linux-env.json after both prepare tools.
Only freshly-created /tmp directories and owned processes are used. Control file
.runtime/ha-control.json accepts kill_primary or stop; status is ha-lab.json.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid

import yaml
import psycopg


def port():
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def main():
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    binaries = Path(json.loads((runtime / "pg-env.json").read_text())["root"])
    root = Path(tempfile.mkdtemp(prefix="video-ha-lab-"))
    bin_dir = binaries / "usr/lib/postgresql/18/bin"
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = str(binaries / "usr/lib/x86_64-linux-gnu")
    env["PATH"] = str(bin_dir) + ":" + env["PATH"]
    nodes, children, logs = [], [], []
    control, status_path = runtime / "ha-control.json", runtime / "ha-lab.json"
    control.unlink(missing_ok=True)
    status_path.unlink(missing_ok=True)
    etcd_ports = [(port(), port()) for _ in range(3)]
    cluster = ",".join(f"etcd-{i}=http://127.0.0.1:{peer}" for i, (_, peer) in enumerate(etcd_ports))
    password = uuid.uuid4().hex

    def spawn(args, name):
        log = (root / (name + ".log")).open("wb")
        logs.append(log)
        process = subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT)
        children.append(process)
        return process

    def state(node):
        try:
            with urlopen(f"http://127.0.0.1:{node['rest_port']}/patroni", timeout=.5) as response:
                return json.load(response)
        except OSError:
            return {}

    def write_status(value):
        temporary = runtime / "ha-lab.tmp"
        temporary.write_text(json.dumps(value))
        os.replace(temporary, status_path)

    try:
        for i, (client, peer) in enumerate(etcd_ports):
            spawn([str(binaries / "usr/bin/etcd"), "--name", f"etcd-{i}",
                   "--data-dir", str(root / f"etcd-{i}"),
                   "--listen-client-urls", f"http://127.0.0.1:{client}",
                   "--advertise-client-urls", f"http://127.0.0.1:{client}",
                   "--listen-peer-urls", f"http://127.0.0.1:{peer}",
                   "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                   "--initial-cluster", cluster, "--initial-cluster-token", root.name], f"etcd-{i}")
        for i in range(3):
            node = {"name": f"pg-{i}", "port": port(), "rest_port": port(), "data": str(root / f"pg-{i}")}
            config = {"scope": "video-lab", "namespace": "/video/", "name": node["name"],
                "restapi": {"listen": f"0.0.0.0:{node['rest_port']}", "connect_address": f"127.0.0.1:{node['rest_port']}"},
                "etcd3": {"hosts": ",".join(f"127.0.0.1:{client}" for client, _ in etcd_ports)},
                "bootstrap": {"dcs": {"ttl": 20, "loop_wait": 5, "retry_timeout": 5,
                    "synchronous_mode": True, "synchronous_mode_strict": True, "synchronous_node_count": 1,
                    "maximum_lag_on_failover": 0,
                    "postgresql": {"use_pg_rewind": True, "parameters": {"synchronous_commit": "on"}}},
                    "initdb": [{"encoding": "UTF8"}, "data-checksums"],
                    "pg_hba": ["host all all 127.0.0.1/32 scram-sha-256", "host replication replicator 127.0.0.1/32 scram-sha-256",
                               "host all all 0.0.0.0/0 scram-sha-256"]},
                "postgresql": {"listen": f"0.0.0.0:{node['port']}", "connect_address": f"127.0.0.1:{node['port']}",
                    "data_dir": node["data"], "bin_dir": str(bin_dir),
                    "authentication": {"superuser": {"username": "postgres", "password": password},
                                       "replication": {"username": "replicator", "password": password}},
                    "parameters": {"unix_socket_directories": str(root)}},
                "watchdog": {"mode": "off"}}
            path = root / (node["name"] + ".yml")
            path.write_text(yaml.safe_dump(config))
            node["process"] = spawn([sys.executable, "-m", "patroni", str(path)], node["name"])
            nodes.append(node)
        deadline = time.monotonic() + 90
        while True:
            states = [state(node) for node in nodes]
            if all(s.get("state") == "running" for s in states) and any(s.get("role") in ("primary", "master") for s in states):
                break
            if time.monotonic() > deadline:
                raise RuntimeError("cluster did not start; logs: " + str(root))
            time.sleep(.5)
        dsn = "host=127.0.0.1,127.0.0.1,127.0.0.1 port=" + ",".join(str(n["port"]) for n in nodes)
        dsn += " dbname=postgres user=postgres password=" + password + " target_session_attrs=read-write"
        deadline = time.monotonic() + 30
        while True:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=3) as connection:
                synchronous = connection.execute("SELECT COUNT(*) FROM pg_stat_replication WHERE sync_state='sync'").fetchone()[0]
            if synchronous:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("synchronous replica did not become ready")
            time.sleep(.2)
        status = {"ready": True, "dsn": dsn, "ports": [n["port"] for n in nodes], "log_root": str(root)}
        write_status(status)
        print("Three-node synchronous PostgreSQL lab ready", flush=True)
        while True:
            if control.exists():
                request = json.loads(control.read_text())
                control.unlink()
                if request["action"] == "stop":
                    break
                if request["action"] == "kill_primary":
                    old = next(n for n in nodes if state(n).get("role") in ("primary", "master"))
                    before = time.monotonic()
                    old["process"].kill()
                    old["process"].wait(timeout=5)
                    subprocess.run([str(bin_dir / "pg_ctl"), "-D", old["data"], "stop", "-m", "immediate"],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    deadline = time.monotonic() + 60
                    while True:
                        new = next((n for n in nodes if n is not old and state(n).get("role") in ("primary", "master")), None)
                        if new:
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("automatic failover did not finish")
                        time.sleep(.1)
                    status.update(old_primary=old["name"], new_primary=new["name"],
                                  promotion_seconds=round(time.monotonic() - before, 3))
                    write_status(status)
                    print("Automatic primary promotion verified", flush=True)
            time.sleep(.1)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for node in nodes:
            subprocess.run([str(bin_dir / "pg_ctl"), "-D", node["data"], "stop", "-m", "immediate"],
                           env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for log in logs:
            log.close()
        # Retain logs but remove credentials from published control/status files.
        for node in nodes:
            (root / (node["name"] + ".yml")).unlink(missing_ok=True)
        if status_path.exists():
            status = json.loads(status_path.read_text())
            status.pop("dsn", None)
            status["stopped"] = True
            write_status(status)


if __name__ == "__main__":
    main()
