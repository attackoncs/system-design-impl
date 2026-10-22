"""Launch the isolated Patroni Python prepared by prepare_patroni_lab.py."""

import json
import os
import pathlib

root = pathlib.Path(__file__).resolve().parents[1]
python = json.loads((root / ".runtime/linux-env.json").read_text())["python"]
os.execv(python, [python, str(root / "tools/patroni_lab.py")])
