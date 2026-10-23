"""Regenerate checked-in gRPC bindings using the pinned grpcio-tools version."""

from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / "src/kv_store/network"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{root / 'proto'}",
            f"--python_out={target}",
            f"--grpc_python_out={target}",
            str(root / "proto/kvstore.proto"),
        ],
        check=True,
    )
    module = target / "kvstore_pb2_grpc.py"
    source = module.read_text(encoding="utf-8")
    module.write_text(
        source.replace(
            "import kvstore_pb2 as kvstore__pb2",
            "from . import kvstore_pb2 as kvstore__pb2",
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
