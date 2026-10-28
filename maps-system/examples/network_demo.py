import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Cluster, request


def main():
    with Cluster() as cluster:
        primary, replica, gateway = cluster.start_all()
        options = {
            "origin": "West Station",
            "destination": "East Station",
            "alternatives": 2,
        }
        initial = request(primary, "/v1/route", "POST", options)
        request(
            primary,
            "/internal/traffic",
            "POST",
            {"records": [{"edge": "bc", "closed": True}]},
            "admin-demo",
        )
        cluster.wait(lambda: request(replica, "/health")["traffic_version"] == 1)
        replanned = request(replica, "/v1/route", "POST", options)
        graph = initial["graph"]
        image, headers = request(replica, f"/v1/tiles/{graph}/0/0/0.png", raw=True)
        vector = request(replica, f"/v1/tiles/{graph}/0/0/0.geojson")
        cluster.stop("primary")
        recovered = request(
            gateway, "/v1/route", "POST", options, headers={"X-Region": "local"}
        )
        print(
            json.dumps(
                {
                    "processes": 3,
                    "independent_databases": 2,
                    "original_edges": initial["routes"][0]["edges"],
                    "original_eta_s": initial["routes"][0]["eta_seconds"],
                    "replanned_edges": replanned["routes"][0]["edges"],
                    "routing_tiles_loaded": initial["routing_tiles_loaded"],
                    "png_bytes": len(image),
                    "vector_features": len(vector["features"]),
                    "primary_failure_recovered_revision": recovered["revision"],
                    "cache_control": headers["Cache-Control"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
