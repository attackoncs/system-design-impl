import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab, wait

parser = argparse.ArgumentParser()
parser.add_argument(
    "--stage", choices=["all", "single", "balanced", "sharded"], default="all"
)
args = parser.parse_args()
for stage in ["single", "balanced", "sharded"] if args.stage == "all" else [args.stage]:
    with Lab(stage) as lab:
        for user in lab.config["users"]:
            lab.login(user)
            lab.update(user=user)
        if stage != "single":
            lab.caught_up()
            lab.drained()
        else:
            wait(
                lambda: (
                    lab.call("/admin/status", node="single", auth="admin")[
                        "pending_jobs"
                    ]
                    == 0
                )
            )
        reads = [lab.call("/v1/profile?min_version=1") for _ in range(3)]
        result = {"stage": stage, "processes": len(lab.processes), "reads": reads}
        if stage != "single":
            result["primaries"] = [
                lab.call("/admin/status", node="primary" + str(i), auth="admin")
                for i in range(len(lab.config["primaries"]))
            ]
            result["consumer"] = lab.call("/admin/status", node="worker", auth="admin")
            result["edge"] = lab.call("/assets/app-v1.txt", node="edge")
        assert all(value["version"] == 1 for value in reads)
        print(json.dumps(result, indent=2))
