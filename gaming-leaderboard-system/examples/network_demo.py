from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab
from gaming_leaderboard.common import current_month, dumps


def main():
    with Lab() as lab:
        for user in ("alice", "bob", "charlie", "loser"):
            lab.register(user)
        start = time.perf_counter()
        body = {
            "match_id": "demo-one",
            "winner": "alice",
            "players": ["alice", "loser"],
            "ended_at": int(time.time()),
        }
        first = lab.api("/v1/scores", body, "game")
        duplicate = lab.api("/v1/scores", body, "game")
        lab.match("demo-two", "bob")
        rows = lab.api("/v1/scores")["rows"]
        elapsed = (time.perf_counter() - start) * 1000
        audit = lab.api(
            "/admin/reconcile", {"month": current_month(time.time())}, "admin"
        )
        print(
            dumps(
                {
                    "processes": 7,
                    "redis_servers": 4,
                    "score_shards": 2,
                    "duplicate_detected": duplicate["duplicate"],
                    "first_projected": first["projected"],
                    "ranks": [r["rank"] for r in rows],
                    "scores": [r["score"] for r in rows],
                    "matches_authority": audit["matches"],
                    "scenario_ms": round(elapsed, 2),
                }
            )
        )


if __name__ == "__main__":
    main()
