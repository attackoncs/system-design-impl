"""Disposable Redis/Sentinel lab (Linux/macOS). Owns and stops only its children.

python tools/redis_lab.py --directory .runtime/lab --binary /path/to/redis-server
Create control.json containing {"kill": 17379} to kill a lab server, or {"stop": true}.
The directory must be new. Never point this tool at production Redis data.
"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--binary", default=shutil.which("redis-server"))
    parser.add_argument("--control-directory", help="optional control/report directory separate from Redis data")
    parser.add_argument("--base-port", type=int, default=17379)
    parser.add_argument("--sentinel-port", type=int, default=27379)
    args = parser.parse_args()
    if not args.binary:
        parser.error("redis-server binary required")
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    control_root = Path(args.control_directory).resolve() if args.control_directory else root
    if control_root != root:
        control_root.mkdir(parents=True, exist_ok=False)
    processes = {}
    try:
        for index in range(6):
            sentinel = index >= 3
            port = (args.sentinel_port + index - 3) if sentinel else (args.base_port + index)
            folder = root / str(port)
            folder.mkdir()
            config = [f"port {port}", "bind 127.0.0.1", "protected-mode yes",
                      f'dir "{folder.as_posix()}"', f'logfile "{(folder / "redis.log").as_posix()}"']
            if sentinel:
                config += [f"sentinel monitor chat-primary 127.0.0.1 {args.base_port} 2",
                           "sentinel down-after-milliseconds chat-primary 1000",
                           "sentinel failover-timeout chat-primary 5000",
                           "sentinel parallel-syncs chat-primary 1"]
            else:
                config += ["appendonly yes", "appendfsync always", "save \"\"",
                           "repl-diskless-sync-delay 0"]
                if index:
                    config.append(f"replicaof 127.0.0.1 {args.base_port}")
            path = folder / "redis.conf"
            path.write_text("\n".join(config) + "\n")
            command = [args.binary, str(path)] + (["--sentinel"] if sentinel else [])
            processes[port] = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        for port, process in processes.items():
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f"Redis process {port} exited; inspect its log")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Redis lab startup timed out")
                    time.sleep(.1)
        (control_root / "ready.json").write_text(json.dumps({"primary": args.base_port,
            "sentinels": [["127.0.0.1", args.sentinel_port + i] for i in range(3)]}))
        print("Redis lab ready: " + str(root), flush=True)
        while True:
            control = control_root / "control.json"
            if control.exists():
                request = json.loads(control.read_text())
                control.unlink()
                if request.get("stop"):
                    break
                port = request.get("kill")
                if port not in processes:
                    raise ValueError("kill target is not owned by this lab")
                processes[port].kill()
                processes[port].wait(timeout=5)
                (control_root / "killed.json").write_text(json.dumps({"port": port}))
            time.sleep(.1)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
