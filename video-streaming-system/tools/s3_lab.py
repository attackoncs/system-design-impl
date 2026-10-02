"""Temporary real SeaweedFS S3 server from a verified official release."""
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen, urlretrieve
import uuid


def port():
    while True:
        candidate = random.randrange(20000, 45000)
        try:
            with socket.socket() as http, socket.socket() as grpc:
                http.bind(("0.0.0.0", candidate))
                grpc.bind(("0.0.0.0", candidate + 10000))
            return candidate
        except OSError:
            pass


def main():
    runtime = Path(__file__).resolve().parents[1] / ".runtime"
    runtime.mkdir(exist_ok=True)
    archive = runtime / "seaweedfs-4.48-linux-amd64.tar.gz"
    if not archive.exists():
        urlretrieve("https://github.com/seaweedfs/seaweedfs/releases/download/4.48/linux_amd64.tar.gz", archive)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != "4a7d108384d044d95212d1342cdda9533fa55842c1c9b41f606ca3c8a9561124":
        raise RuntimeError("SeaweedFS official asset checksum mismatch")
    root = Path(tempfile.mkdtemp(prefix="video-s3-lab-"))
    binary = root / "weed"
    with tarfile.open(archive) as bundle:
        member = next(m for m in bundle.getmembers() if Path(m.name).name == "weed" and m.isfile())
        with bundle.extractfile(member) as source:
            binary.write_bytes(source.read())
    binary.chmod(0o700)
    ports = [port() for _ in range(4)]
    access, secret = "video-lab", uuid.uuid4().hex
    config = root / "s3.json"
    config.write_text(json.dumps({"identities": [{"name": "video-lab", "credentials": [{"accessKey": access,
        "secretKey": secret}], "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}))
    data = root / "data"
    data.mkdir()
    endpoint = f"http://127.0.0.1:{ports[3]}"
    status = {"endpoint": endpoint, "access_key": access, "secret_key": secret,
              "version": "SeaweedFS 4.48", "log_root": str(root)}
    stop = runtime / "s3-stop"
    stop.unlink(missing_ok=True)
    with (root / "server.log").open("wb") as log:
        child = subprocess.Popen([str(binary), "server", "-dir=" + str(data), "-ip=127.0.0.1", "-ip.bind=0.0.0.0",
            f"-master.port={ports[0]}", f"-volume.port={ports[1]}", "-volume.max=100", "-master.volumeSizeLimitMB=64",
            "-filer", f"-filer.port={ports[2]}", "-s3", f"-s3.port={ports[3]}", "-s3.config=" + str(config)],
            cwd=root, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(endpoint, timeout=.5):
                        break
                except HTTPError as error:
                    if error.code == 403:
                        break  # S3 requires credentials, as intended.
                    raise
                except OSError:
                    if child.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("S3 server did not start; logs: " + str(root))
                    time.sleep(.1)
            (runtime / "s3-lab.json").write_text(json.dumps(status))
            print("Real temporary SeaweedFS S3 server ready", flush=True)
            while not stop.exists():
                if child.poll() is not None:
                    raise RuntimeError("S3 server exited")
                time.sleep(.2)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            (runtime / "s3-lab.json").unlink(missing_ok=True)
            stop.unlink(missing_ok=True)
            config.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
