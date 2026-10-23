"""Run Redis integration tests against a newly owned loopback-only lab.

Set NEARBY_REDIS_BINARY or NEARBY_WSL_REDIS_BINARY (and optional WSL library
path) as described by nearby-friends-system. Never uses an existing Redis.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "projects", nargs="*", choices=["chat-system", "nearby-friends-system"]
    )
    projects = parser.parse_args().projects or ["chat-system", "nearby-friends-system"]
    sys.path.insert(0, str(ROOT / "nearby-friends-system" / "src"))
    spec = importlib.util.spec_from_file_location(
        "review_redis_lab", ROOT / "nearby-friends-system" / "tools" / "lab.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lab = module.Cluster()
    try:
        lab.start_redis()
        env = dict(os.environ, CHAT_TEST_REDIS_URL=lab.urls[0], PYTHONUTF8="1")
        command = [
            sys.executable,
            str(ROOT / "tools" / "run_project_tests.py"),
            *projects,
            "--jobs",
            "1",
            "--output",
            str(ROOT / "scale-from-zero" / ".runtime" / "review-redis"),
        ]
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    finally:
        lab.close()


if __name__ == "__main__":
    raise SystemExit(main())
