import base64
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from lab import Lab, wait_for
from distributed_email.common import dumps


def main():
    with Lab() as lab:
        upload = lab.api(
            "/v1/uploads",
            {
                "filename": "demo.bin",
                "data": base64.b64encode(b"local attachment").decode(),
            },
        )
        start = time.perf_counter()
        first = lab.send(attachments=[upload["upload_id"]])
        again = lab.send(attachments=[upload["upload_id"]])
        wait_for(lab.settled)
        wait_for(lab.caught_up)
        inbox = lab.api(
            "/v1/messages?q=searchable&field=body&replica=true", user=lab.recipient
        )
        mail = inbox["messages"][0]
        attachment = lab.api(
            f"/v1/messages/{mail['id']}/attachments/0", user=lab.recipient
        )
        elapsed = (time.perf_counter() - start) * 1000
        print(
            dumps(
                {
                    "processes": 9,
                    "external_delivery": False,
                    "same_id_on_retry": first["id"] == again["id"],
                    "unique_inbox_messages": len(inbox["messages"]),
                    "replica_search_source": inbox["source"],
                    "attachment_matches": base64.b64decode(attachment["data"])
                    == b"local attachment",
                    "send_to_replica_search_ms": round(elapsed, 2),
                    "scan_engine": mail["descriptor"]["scan_engine"],
                }
            )
        )


if __name__ == "__main__":
    main()
