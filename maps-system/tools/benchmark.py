from concurrent.futures import ThreadPoolExecutor
import argparse
import json
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent),
]
from lab import Cluster, request
from maps_service.store import Store
from maps_service.navigation import route


def grid(size):
    nodes = [
        {"id": f"n{x}_{y}", "latitude": y * 0.001, "longitude": x * 0.001}
        for x in range(size)
        for y in range(size)
    ]
    edges = []
    for x in range(size):
        for y in range(size):
            for xx, yy in [(x + 1, y), (x, y + 1)]:
                if xx >= size or yy >= size:
                    continue
                for a, b in [
                    (f"n{x}_{y}", f"n{xx}_{yy}"),
                    (f"n{xx}_{yy}", f"n{x}_{y}"),
                ]:
                    edges.append(
                        {
                            "id": a + "_" + b,
                            "source": a,
                            "target": b,
                            "length_m": 111.2,
                            "speeds": {"driving": 10, "walking": 1.4, "cycling": 5},
                        }
                    )
    return {"license": "CC0 synthetic grid", "nodes": nodes, "edges": edges}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--output", default=".runtime/benchmark.json")
    args = parser.parse_args()
    if (
        not 3 <= args.size <= 100
        or not 6 <= args.requests <= 1000
        or not 1 <= args.clients <= 16
    ):
        parser.error("size 3..100, requests 6..1000, clients 1..16 required")
    with Cluster() as cluster:
        store = Store(cluster.root / "primary.sqlite")
        graph = grid(args.size)
        started = time.perf_counter()
        store.import_graph(graph)
        import_ms = (time.perf_counter() - started) * 1000
        with store.transaction() as db:
            all_tiles = db.execute(
                "SELECT count(*) FROM (SELECT DISTINCT tx,ty FROM nodes)"
            ).fetchone()[0]
        local_route = route(store, "n0_0", "n1_1")
        primary = cluster.start("primary", "serve")
        version = request(primary, "/health")["graph"]
        cases = [
            (
                "route",
                "/v1/route",
                "POST",
                {"origin": "n0_0", "destination": f"n{args.size - 1}_{args.size - 1}"},
            ),
            ("geojson", f"/v1/tiles/{version}/14/8192/8191.geojson", "GET", None),
            ("png", f"/v1/tiles/{version}/14/8192/8191.png", "GET", None),
        ]

        def query(index):
            name, path, method, body = cases[index % 3]
            start = time.perf_counter()
            data, headers = request(primary, path, method, body, raw=True)
            return name, (time.perf_counter() - start) * 1000, len(data)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.clients) as executor:
            results = list(executor.map(query, range(args.requests)))
        elapsed = time.perf_counter() - started
        report = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "nodes": len(graph["nodes"]),
            "directed_edges": len(graph["edges"]),
            "import_ms": round(import_ms, 3),
            "requests": args.requests,
            "clients": args.clients,
            "seconds": round(elapsed, 3),
            "mixed_qps": round(args.requests / elapsed, 2),
            "errors": 0,
            "sqlite_bytes": sum(
                p.stat().st_size for p in cluster.root.glob("primary.sqlite*")
            ),
            "scope": "independent localhost HTTP primary; synthetic grid; mixed route/vector/PNG workload; not global traffic or CDN",
            "cases": [],
        }
        for name, _, _, _ in cases:
            samples = sorted(r[1] for r in results if r[0] == name)
            report["cases"].append(
                {
                    "case": name,
                    "count": len(samples),
                    "p50_ms": round(statistics.median(samples), 3),
                    "p95_ms": round(samples[int(0.95 * (len(samples) - 1))], 3),
                    "response_bytes": next(r[2] for r in results if r[0] == name),
                }
            )
        report["all_graph_routing_tiles"] = all_tiles
        report["short_route_tiles_loaded"] = local_route["routing_tiles_loaded"]
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
