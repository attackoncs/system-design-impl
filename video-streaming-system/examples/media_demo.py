"""Generate, upload, transcode and decode encrypted video over local HTTP."""
from pathlib import Path
import runpy
import sys


if __name__ == "__main__":
    sys.argv = ["benchmark.py", "--http-requests", "10"]
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "benchmark.py"), run_name="__main__")
