"""Verify mailbox projections over the actual nine-process lab."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from lab import Lab, wait_for


def run():
    with Lab() as lab:
        initial = lab.api("/v1/changes", user=lab.recipient)
        query = urlencode(
            {"after": initial["position"], "lineage": initial["lineage"], "wait": 2}
        )
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(lab.api, "/v1/changes?" + query, user=lab.recipient)
            sent = lab.send("notification-demo", body="durable notification example")
            notice = pending.result(timeout=5)
        if not notice["events"]:
            notice = wait_for(
                lambda: (
                    value
                    if (value := lab.api("/v1/changes?" + query, user=lab.recipient))[
                        "events"
                    ]
                    else None
                )
            )
        assert len(notice["events"]) == 1
        mail = notice["events"][0]["id"]
        notified_ms = (time.perf_counter() - started) * 1000
        duplicate = lab.send("notification-demo", body="durable notification example")
        assert sent["id"] == duplicate["id"]
        folder = lab.api("/v1/folders", user=lab.recipient)
        assert folder["folders"][0]["unread"] == 1
        page = lab.api("/v1/folders/inbox/messages", user=lab.recipient)
        assert page["messages"][0]["id"] == mail
        read = lab.api(
            "/v1/messages/" + mail,
            {"version": 1, "read": True},
            user=lab.recipient,
            method="PUT",
        )
        wait_for(lab.caught_up)
        barrier = urlencode(
            {
                "min_position": read["position"],
                "lineage": read["lineage"],
                "replica": "true",
            }
        )
        replica = lab.api("/v1/messages/" + mail + "?" + barrier, user=lab.recipient)
        assert replica["read"] == 1 and replica["source"] == "replica"
        wrong_lineage_status = None
        try:
            lab.api("/v1/folders?min_position=0&lineage=replaced", user=lab.recipient)
        except HTTPError as exc:
            wrong_lineage_status = exc.code
            exc.close()
        assert wrong_lineage_status == 409
        deleted = lab.api(
            "/v1/messages/" + mail + "?version=2", user=lab.recipient, method="DELETE"
        )
        wait_for(lab.caught_up)
        changes = lab.api(
            "/v1/changes?"
            + urlencode(
                {
                    "after": notice["position"],
                    "lineage": notice["lineage"],
                    "replica": "true",
                }
            ),
            user=lab.recipient,
        )
        assert [event["version"] for event in changes["events"]] == [2, 3]
        assert changes["events"][-1]["deleted"]
        assert lab.api("/v1/changes", user="outsider")["events"] == []
        for _ in range(2):
            lab.api("/v1/folders", user=lab.recipient)
        cache = lab.call("primary-1", "/internal/status", user="internal")[
            "metadata_cache"
        ]
        assert cache["hits"] >= 1
        wait_for(lab.settled)
        return {
            "processes": 9,
            "notification_ms": round(notified_ms, 3),
            "inbox_events": len(notice["events"]),
            "same_send_id": True,
            "folder_unread_before_read": 1,
            "replica_read_barrier_version": replica["version"],
            "wrong_lineage_status": wrong_lineage_status,
            "resumed_versions": [event["version"] for event in changes["events"]],
            "deleted_version": deleted["version"],
            "outsider_events": 0,
            "metadata_cache_hits": cache["hits"],
            "scope": "localhost HTTP/SMTP, durable log and metadata replicas; test marker scanner",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="benchmarks/features-2026-10-26.json")
    result = run()
    path = Path(parser.parse_args().output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
