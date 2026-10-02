"""Download/extract Ubuntu lab binaries locally; never install system services."""
from pathlib import Path
import os
import subprocess
import json
import tempfile


root = Path(__file__).resolve().parents[1] / ".runtime" / "pg-packages"
root.mkdir(parents=True, exist_ok=True)
packages = ["postgresql-18", "postgresql-client-18", "libpq5", "etcd-server", "libllvm21", "libicu78",
            "libxml2-16", "libldap2", "libsasl2-2", "libnuma1", "liburing2"]
missing = [package for package in packages if not list(root.glob(package + "_*.deb"))]
if missing:
    subprocess.run(["apt-get", "download"] + missing, cwd=root, check=True)
extracted = Path(tempfile.mkdtemp(prefix="video-postgres-bin-"))
for archive in root.glob("*.deb"):
    subprocess.run(["dpkg-deb", "-x", str(archive), str(extracted)], check=True)
env = os.environ.copy()
env["LD_LIBRARY_PATH"] = str(extracted / "usr/lib/x86_64-linux-gnu")
subprocess.run(["ldd", str(extracted / "usr/lib/postgresql/18/bin/postgres")], env=env, check=True)
(root.parent / "pg-env.json").write_text(json.dumps({"root": str(extracted)}))
