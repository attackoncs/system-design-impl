import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import platform
import time
from lab import Lab

parser = argparse.ArgumentParser(
    description="Compare small real-HTTP read samples across scaling stages"
)
parser.add_argument("--requests", type=int, default=30)
parser.add_argument("--clients", type=int, default=4)
parser.add_argument("--output", default="benchmarks/latest.json")
args = parser.parse_args()
if not 2 <= args.requests <= 1000 or not 1 <= args.clients <= 16:
    parser.error("reference supports 2..1000 requests and 1..16 clients")
samples = []
for stage in ("single", "balanced", "sharded"):
    with Lab(stage) as lab:
        for user in lab.config["users"]:
            lab.login(user)
            lab.update(user=user)
        lab.caught_up()
        lab.call("/v1/profile?min_version=1")

        def primary_reads():
            nodes = (
                ["single"]
                if stage == "single"
                else ["primary" + str(i) for i in range(len(lab.config["primaries"]))]
            )
            return sum(
                lab.call("/admin/status", node=node, auth="admin")["profile_reads"]
                for node in nodes
            )

        before = primary_reads()

        def read(_):
            start = time.perf_counter()
            value = lab.call("/v1/profile")
            assert value["version"] == 1
            return (time.perf_counter() - start) * 1000

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.clients) as pool:
            latency = sorted(pool.map(read, range(args.requests)))
        elapsed = time.perf_counter() - started
        samples.append(
            {
                "stage": stage,
                "processes": len(lab.processes),
                "requests": args.requests,
                "clients": args.clients,
                "elapsed_seconds": elapsed,
                "reads_per_second": args.requests / elapsed,
                "p50_ms": latency[math.ceil(len(latency) * 0.5) - 1],
                "p95_ms": latency[math.ceil(len(latency) * 0.95) - 1],
                "additional_primary_profile_reads": primary_reads() - before,
                "cache": lab.call("/admin/status", node="cache", auth="admin")
                if stage != "single"
                else None,
            }
        )
result = {
    "python": platform.python_version(),
    "platform": platform.platform(),
    "samples": samples,
    "scope": "Small warmed-cache loopback samples with sessions, excludes startup/seed/warmup; added tiers incur network overhead. Not a million-user capacity proof or production speedup.",
}
path = Path(args.output)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
