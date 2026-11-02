"""Run strict chat tests against a short-lived, authenticated, owned Redis.

Uses redis-server on PATH/CHAT_REDIS_BINARY, or the existing extracted WSL runtime.
No existing Redis URL or Sentinel fault-injection configuration is adopted.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

ROOT = Path(__file__).resolve().parents[1]


def wsl_path(path):
    value = path.resolve()
    return "/mnt/" + value.drive[0].lower() + value.as_posix()[2:]


def command(folder, port, password):
    binary = os.environ.get("CHAT_REDIS_BINARY") or shutil.which("redis-server")
    options = [
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--requirepass",
        password,
        "--protected-mode",
        "yes",
        "--save",
        "",
        "--appendonly",
        "yes",
        "--appendfsync",
        "always",
        "--maxmemory-policy",
        "noeviction",
    ]
    if binary:
        return [binary, *options, "--dir", str(folder)]
    extracted = ROOT / ".runtime/extracted/usr/bin/redis-server"
    if os.name != "nt" or not extracted.is_file():
        raise RuntimeError(
            "set CHAT_REDIS_BINARY or install redis-server; no Redis mock is used"
        )
    library = ROOT / ".runtime/extracted/usr/lib/x86_64-linux-gnu"
    return [
        "wsl",
        "-d",
        os.environ.get("CHAT_WSL_DISTRO", "Ubuntu-26.04"),
        "--",
        "env",
        "LD_LIBRARY_PATH=" + wsl_path(library),
        wsl_path(extracted),
        *options,
        "--dir",
        wsl_path(folder),
    ]


def run(output):
    folder = ROOT / ".runtime" / ("review-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    password = secrets.token_urlsafe(24)
    client = redis.Redis(
        host="127.0.0.1",
        port=port,
        password=password,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        retry=Retry(NoBackoff(), 0),
    )
    process, run_id, report = None, None, None
    stopped = False
    with (folder / "redis.log").open("wb") as log:
        try:
            process = subprocess.Popen(
                command(folder, port, password),
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.monotonic() + 20
            while run_id is None:
                try:
                    info = client.info("server")
                    run_id = info["run_id"]
                except redis.RedisError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError(
                            "owned Redis startup failed; inspect "
                            + str(folder / "redis.log")
                        )
                    time.sleep(0.1)
            env = dict(
                os.environ,
                CHAT_TEST_REDIS_URL=f"redis://:{password}@127.0.0.1:{port}/0",
                PYTHONPATH=str(ROOT / "src"),
                PYTHONUTF8="1",
                PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
                TEMP=str(ROOT.parent / ".test-tmp"),
                TMP=str(ROOT.parent / ".test-tmp"),
            )
            env.pop("CHAT_TEST_REDIS_LAB", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "error",
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp=" + str(folder / "pytest"),
                    "--junitxml=" + str(folder / "tests.xml"),
                ],
                cwd=ROOT,
                env=env,
                timeout=180,
                check=False,
            )
            suites = ET.parse(folder / "tests.xml").getroot()
            suite = next(suites.iter("testsuite"))
            counts = {
                name: int(suite.attrib[name])
                for name in ("tests", "failures", "errors", "skipped")
            }
            report = {
                "python": platform.python_version(),
                "platform": platform.system(),
                "redis_version": info["redis_version"],
                "warnings_as_errors": True,
                **counts,
                "passed": counts["tests"]
                - counts["failures"]
                - counts["errors"]
                - counts["skipped"],
                "seconds": float(suite.attrib["time"]),
                "pytest_exit_code": result.returncode,
                "skipped_cases": [
                    {
                        "name": case.attrib["name"],
                        "reason": skipped.attrib.get("message", ""),
                    }
                    for case in suite.iter("testcase")
                    if (skipped := case.find("skipped")) is not None
                ],
                "scope": "Owned Redis with AOF/always and real loopback WebSockets; Sentinel primary-crash experiment excluded.",
            }
        finally:
            try:
                if run_id is not None and client.info("server")["run_id"] == run_id:
                    client.shutdown(nosave=True)
                    stopped = True
            except redis.RedisError:
                pass
            finally:
                client.close()
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    report["owned_redis_shutdown"] = stopped and process.poll() is not None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report["pytest_exit_code"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output))
