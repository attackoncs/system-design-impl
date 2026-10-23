"""Run chapter suites in isolated interpreters and preserve their full logs.

Each project must have its test dependencies installed in this interpreter.
Real-service suites retain their documented environment prerequisites.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run_project(project: Path, output: Path) -> dict:
    """Run one suite with isolated imports, temp files and a durable log."""
    temp = ROOT / ".test-tmp" / hashlib.sha256(project.name.encode()).hexdigest()[:8]
    temp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(project / "src"),
        PYTHONUTF8="1",
        TEMP=str(temp),
        TMP=str(temp),
        HYPOTHESIS_STORAGE_DIRECTORY=str(output / project.name / "hypothesis"),
    )
    log = output / f"{project.name}.log"
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
            cwd=project,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return dict(
        project=project.name,
        exit_code=result.returncode,
        seconds=round(time.monotonic() - start, 2),
        log=str(log),
        summary=log.read_text(encoding="utf-8", errors="replace").splitlines()[-4:],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("projects", nargs="*")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    available = {
        p.name: p
        for p in ROOT.iterdir()
        if (p / "pyproject.toml").is_file() and (p / "tests").is_dir()
    }
    unknown = set(args.projects) - available.keys()
    if unknown or args.jobs < 1:
        parser.error(f"invalid projects/jobs: {sorted(unknown)}")
    selected = [available[name] for name in (args.projects or sorted(available))]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / "results.json"
    previous = (
        json.loads(report_path.read_text(encoding="utf-8"))
        if report_path.exists()
        else []
    )
    names = {project.name for project in selected}
    results = [result for result in previous if result["project"] not in names]
    current = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_project, project, output) for project in selected]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            current.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            (output / "results.json").write_text(
                json.dumps(sorted(results, key=lambda item: item["project"]), indent=2),
                encoding="utf-8",
            )
    return int(any(item["exit_code"] for item in current))


if __name__ == "__main__":
    raise SystemExit(main())
