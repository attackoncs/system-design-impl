from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Cluster, request


def main():
    with Cluster() as cluster:
        primary = cluster.start("primary", "primary")
        created = request(
            primary,
            "/v1/businesses",
            "POST",
            {
                "name": "Dateline Cafe",
                "latitude": 0,
                "longitude": 179.999,
                "category": "cafe",
            },
            "alice-demo",
            "demo-create",
        )
        query = "/v1/search/nearby?latitude=0&longitude=-179.999&radius=500"
        before = request(primary, query)
        published = request(primary, "/internal/publish", "POST", token="admin-demo")
        replicas = [
            cluster.start(name, "replica", ["--upstream", primary, "--interval", "0.1"])
            for name in ["r1", "r2"]
        ]
        for replica in replicas:
            cluster.wait(
                lambda replica=replica: (
                    request(replica, "/health")["version"] == published["version"]
                )
            )
        config = cluster.root / "regions.json"
        config.write_text(json.dumps({"local": replicas}), encoding="utf-8")
        gateway = cluster.start("gateway", "gateway", ["--regions", str(config)])
        after = request(gateway, query, region="local")
        cluster.stop("r1")
        failover = request(gateway, query, region="local")
        cluster.stop("primary")
        offline_primary = request(replicas[1], query)
        print(
            json.dumps(
                {
                    "created_id": created["id"],
                    "pending_total": before["total"],
                    "published_total": after["total"],
                    "distance_m": after["businesses"][0]["distance_m"],
                    "replica_failure_total": failover["total"],
                    "primary_failure_total": offline_primary["total"],
                    "independent_sqlite_files": 3,
                    "service_processes": 4,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
