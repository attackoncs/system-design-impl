"""Repeatable process/network synchronization report, not a capacity claim."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from network_demo import run_demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".runtime/sync.json")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))
