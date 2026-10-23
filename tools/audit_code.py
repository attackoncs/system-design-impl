"""Dependency-free repository audit; signals require human review.

Run from any directory: python tools/audit_code.py --output report.json
Only chapter src trees are inspected; runtime data and environments are skipped.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def inspect_repository(root: Path) -> dict:
    """Return syntax errors, complexity signals and exact function duplicates."""
    projects = []
    findings = []
    duplicates = defaultdict(list)
    for project in sorted(root.iterdir()):
        source = project / "src"
        if not (project / "pyproject.toml").is_file() or not source.is_dir():
            continue
        files = sorted(source.rglob("*.py"))
        lines = 0
        for path in files:
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8-sig")
            length = len(text.splitlines())
            lines += length
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as error:
                findings.append(
                    dict(
                        kind="syntax_error",
                        file=relative,
                        line=error.lineno,
                        detail=error.msg,
                    )
                )
                continue
            if length > 500:
                findings.append(
                    dict(kind="large_module", file=relative, line=1, detail=length)
                )
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    size = node.end_lineno - node.lineno + 1
                    if size > 80:
                        findings.append(
                            dict(
                                kind="long_function",
                                file=relative,
                                line=node.lineno,
                                detail=f"{node.name}: {size} lines",
                            )
                        )
                    if size >= 10:
                        digest = hashlib.sha256(ast.dump(node).encode()).hexdigest()
                        duplicates[digest].append(
                            dict(file=relative, line=node.lineno, name=node.name)
                        )
                if isinstance(node, ast.ExceptHandler):
                    broad = node.type is None or (
                        isinstance(node.type, ast.Name)
                        and node.type.id in {"Exception", "BaseException"}
                    )
                    if broad:
                        findings.append(
                            dict(
                                kind="broad_exception",
                                file=relative,
                                line=node.lineno,
                                detail="Review recovery/rollback boundary; not automatically a bug",
                            )
                        )
        projects.append(dict(name=project.name, files=len(files), lines=lines))
    return dict(
        projects=projects,
        findings=findings,
        duplicate_functions=[group for group in duplicates.values() if len(group) > 1],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = inspect_repository(ROOT)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
    return int(any(item["kind"] == "syntax_error" for item in report["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
