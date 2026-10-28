"""Offline tiles and private adaptive navigation over a real primary process."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from lab import Cluster, request


def event_data(payload):
    events = [
        json.loads(line[6:])
        for line in payload.decode().splitlines()
        if line.startswith("data: ")
    ]
    if not events:
        raise RuntimeError("expected a navigation route event")
    return events[-1]


def run():
    with Cluster() as cluster:
        archive = cluster.root / "tiles.sqlite"
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "maps_service.cli",
                "precompute",
                "--db",
                str(cluster.root / "primary.sqlite"),
                "--tiles",
                str(ROOT / "examples/tiles.json"),
                "--tile-archive",
                str(archive),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        prepared = json.loads(completed.stdout)
        primary = cluster.start("primary", "serve", ["--tile-archive", str(archive)])
        urls = request(primary, "/v1/tile-urls?latitude=0&longitude=0&zoom=14")
        created = request(
            primary,
            "/v1/navigation",
            "POST",
            {"origin": "A", "destination": "C"},
            "alice-demo",
        )
        session = created["id"]
        first = event_data(
            request(
                primary,
                f"/v1/navigation/{session}/events",
                token="alice-demo",
                raw=True,
            )[0]
        )
        before = first["route"]["routes"][0]
        request(
            primary,
            "/internal/traffic",
            "POST",
            {"records": [{"edge": "bc", "closed": True}]},
            "admin-demo",
        )
        updated = event_data(
            request(
                primary,
                f"/v1/navigation/{session}/events?after={first['revision']}",
                token="alice-demo",
                raw=True,
            )[0]
        )
        after = updated["route"]["routes"][0]
        assert before["edges"] == ["ab", "bc"]
        assert after["edges"] == ["ad", "de", "ec"]
        payload, headers = request(
            primary, f"/v1/tiles/{prepared['graph']}/0/0/0.png", raw=True
        )
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
        removed = request(
            primary, f"/v1/navigation/{session}", "DELETE", token="alice-demo"
        )
        return {
            "scope": "synthetic roads, independent localhost primary and CLI precompute; not a live map provider",
            "precomputed_tiles": prepared["tiles"],
            "precomputed_objects": prepared["objects"],
            "precomputed_bytes": prepared["bytes"],
            "viewport_urls": len(urls["tiles"]),
            "initial_edges": before["edges"],
            "rerouted_edges": after["edges"],
            "steps": len(after["steps"]),
            "encoded_polyline": bool(after["polyline"]["points"]),
            "sse_revision": updated["revision"],
            "png_bytes": len(payload),
            "png_type": headers["Content-Type"],
            "session_removed": removed["removed"],
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run()
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
