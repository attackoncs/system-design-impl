"""Install isolated Linux Patroni lab dependencies in a newly owned /tmp
venv.
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import urllib.request

runtime = pathlib.Path(__file__).resolve().parents[1] / ".runtime"
runtime.mkdir(exist_ok=True)
root = pathlib.Path(tempfile.mkdtemp(prefix="video-patroni-env-"))
subprocess.run(
    [sys.executable, "-m", "venv", "--without-pip", str(root)], check=True
)
bootstrap = runtime / "get-pip.py"
urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", bootstrap)
python = root / "bin/python"
subprocess.run([str(python), str(bootstrap)], check=True)
subprocess.run(
    [
        str(python),
        "-m",
        "pip",
        "install",
        "patroni[etcd3]>=4,<5",
        "psycopg[binary]>=3.2,<4",
    ],
    check=True,
)
(runtime / "linux-env.json").write_text(json.dumps({"python": str(python)}))
print("Isolated Patroni environment ready")
