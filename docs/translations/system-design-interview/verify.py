"""Verify structural fidelity against the user-supplied local English manuscript."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
FILES = {
    17: "17-proximity-service.zh-CN.md",
    18: "18-nearby-friends.zh-CN.md",
    19: "19-google-maps.zh-CN.md",
    20: "20-distributed-message-queue.zh-CN.md",
    22: "22-ad-click-event-aggregation.zh-CN.md",
    23: "23-hotel-reservation-system.zh-CN.md",
    24: "24-distributed-email-service.zh-CN.md",
    25: "25-s3-like-object-storage.zh-CN.md",
    26: "26-real-time-gaming-leaderboard.zh-CN.md",
    27: "27-payment-system.zh-CN.md",
    28: "28-digital-wallet.zh-CN.md",
    29: "29-stock-exchange.zh-CN.md",
    30: "30-the-learning-continues.zh-CN.md",
}
PAPER_CHAPTERS = {21: "指标监控与告警系统", 27: "支付系统"}
REFERENCE_ONLY = {27}
READING_FILES = {
    number: filename
    for number, filename in FILES.items()
    if number not in REFERENCE_ONLY
}


def chapters(text):
    matches = list(re.finditer(r"^# (.+)$", text, re.M))
    result = {}
    for index, match in enumerate(matches):
        number = int(re.search(r"\d+", match[1])[0])
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[number] = text[match.start() : end]
    return result


def urls(line):
    return re.findall(r"https?://[^\s<>\)\]`'\"“”‘’，。、；]+", line)


def verify(source_path):
    source_bytes = source_path.read_bytes()
    source = chapters(source_bytes.decode("utf-8"))
    report = {
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "chapters": [],
        "errors": [],
        "untranslated_candidates": [],
        "reading_chapters": list(READING_FILES),
        "already_in_paper": sorted(PAPER_CHAPTERS),
        "retained_reference_only": sorted(REFERENCE_ONLY),
    }
    expected_order = [
        number for number in range(17, 31) if number not in PAPER_CHAPTERS
    ]
    if list(READING_FILES) != expected_order:
        report["errors"].append(
            {
                "error": "reading coverage/order differs from the paper-missing chapter list"
            }
        )
    for number, filename in FILES.items():
        original = source[number].splitlines()
        translated = (HERE / filename).read_text(encoding="utf-8").splitlines()
        row = {
            "chapter": number,
            "file": filename,
            "source_lines": len(original),
            "translated_lines": len(translated),
            "headings": 0,
            "images": 0,
            "table_lines": 0,
            "code_lines": 0,
            "fences": 0,
            "url_occurrences": 0,
            "inline_code_segments": 0,
            "sha256": hashlib.sha256((HERE / filename).read_bytes()).hexdigest(),
        }
        if len(original) != len(translated):
            report["errors"].append({"chapter": number, "error": "line count differs"})
        fence = None
        for index, (a, b) in enumerate(zip(original, translated), 1):

            def error(message):
                report["errors"].append(
                    {"chapter": number, "source_line": index, "error": message}
                )

            marker = re.match(r"^\s*(`{3,}|~{3,})", a)
            if marker:
                row["fences"] += 1
                if a != b:
                    error("code fence changed")
                fence = None if fence else marker[1]
                continue
            if fence:
                row["code_lines"] += 1
                if a != b:
                    error("code block changed")
                continue
            if bool(a.strip()) != bool(b.strip()):
                error("blank line position changed")
            if re.match(r"^\s*", a)[0] != re.match(r"^\s*", b)[0]:
                error("indent changed")
            heading = re.match(r"^(#{1,6})\s", a)
            if heading:
                row["headings"] += 1
                if not b.startswith(heading[1] + " "):
                    error("heading level changed")
            item = re.match(r"^(\s*(?:[-*+]\s+|\d+(?:\\)?\.\s+))", a)
            if item and not b.startswith(item[1]):
                error("list marker changed")
            if a.lstrip().startswith("|"):
                row["table_lines"] += 1
                if len(re.findall(r"(?<!\\)\|", a)) != len(re.findall(r"(?<!\\)\|", b)):
                    error("table column count changed")
            if "![" in a:
                row["images"] += a.count("![")
                if a.count("![") != b.count("!["):
                    error("image count changed")
            else:
                inline = re.findall(r"(?<!`)`([^`]+)`(?!`)", a)
                row["inline_code_segments"] += len(inline)
                if Counter(inline) != Counter(re.findall(r"(?<!`)`([^`]+)`(?!`)", b)):
                    error("inline code changed")
            row["url_occurrences"] += len(urls(a))
            if urls(a) != urls(b):
                error("URL changed")
            if (
                a == b
                and re.search(
                    r"\b(the|this|these|that|when|because|should|we|which)\b", a, re.I
                )
                and not a.lstrip().startswith(("[", "|"))
            ):
                report["untranslated_candidates"].append(
                    {"chapter": number, "source_line": index, "text": a}
                )
        if fence:
            report["errors"].append(
                {"chapter": number, "error": "unclosed code fence in source"}
            )
        report["chapters"].append(row)
    reading_rows = [
        row for row in report["chapters"] if row["chapter"] in READING_FILES
    ]
    report["reading_totals"] = {
        key: sum(row[key] for row in reading_rows)
        for key in (
            "source_lines",
            "headings",
            "images",
            "table_lines",
            "code_lines",
            "fences",
            "url_occurrences",
            "inline_code_segments",
        )
    }
    report["status"] = (
        "passed"
        if not report["errors"] and not report["untranslated_candidates"]
        else "review_required"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source", type=Path, default=HERE.parents[2] / "System Design Interview.md"
    )
    parser.add_argument("--output", type=Path, default=HERE / "verification.json")
    args = parser.parse_args()
    result = verify(args.source)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "chapters": len(result["chapters"]),
                "errors": len(result["errors"]),
                "untranslated_candidates": len(result["untranslated_candidates"]),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
