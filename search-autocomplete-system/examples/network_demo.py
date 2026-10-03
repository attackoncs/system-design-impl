"""Owns three HTTP shard processes and one coordinator; kills one replica."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

from search_autocomplete import Store


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(port):
    with urlopen(f"http://127.0.0.1:{port}/suggest?prefix=tw", timeout=5) as response:
        return json.load(response)


def main():
    children = []
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        db = Path(directory) / "demo.sqlite3"
        store = Store(db)
        try:
            for query, count in [("twitter", 35), ("twitch", 29), ("twilight", 25),
                                 ("twin peak", 21), ("twitch prime", 18), ("twitter search", 14)]:
                for i in range(count):
                    store.record(f"{query}-{i}", query, 100)
            store.build(0, 200, 2)
            ports = [free_port() for _ in range(4)]
            replicas = {"0": [f"http://127.0.0.1:{port}" for port in ports[:2]],
                        "1": [f"http://127.0.0.1:{ports[2]}"]}
            mapping = Path(directory) / "replicas.json"
            mapping.write_text(json.dumps(replicas), encoding="utf-8")
            commands = [["node", "--shard", str(shard), "--port", str(port)]
                        for port, shard in zip(ports[:3], [0, 0, 1])]
            commands.append(["coordinator", "--replicas", str(mapping), "--port", str(ports[3])])
            for port, command in zip(ports, commands):
                child = subprocess.Popen([sys.executable, "-m", "search_autocomplete.cli", "--db", str(db)] + command,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                children.append(child)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fetch(port)
                        break
                    except OSError:
                        if child.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("demo process failed to start")
                        time.sleep(.05)
            print("Two-shard result:", fetch(ports[3])["suggestions"])
            children[0].kill()
            children[0].wait(timeout=5)
            print("After first replica crash:", fetch(ports[3])["suggestions"])
            store.block("twitter")
            print("After immediate removal:", fetch(ports[3])["suggestions"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
            store.close()


if __name__ == "__main__":
    main()
