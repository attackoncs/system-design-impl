"""Claim synthetic PSP callbacks without holding a writer during network I/O."""

import json
import time
import uuid

from .common import dumps, http_json, signature


def dispatch_once(store, *, deadline=None, cancel=None):
    deadline = time.monotonic() + 5 if deadline is None else deadline
    if cancel is not None and cancel.is_set():
        return False
    with store.transaction(True) as db:
        now = store.clock()
        if db.execute("SELECT webhooks_down FROM control").fetchone()[0]:
            return False
        row = db.execute(
            "SELECT * FROM webhooks WHERE (status='pending' AND due<=?) OR (status='inflight' AND lease<=?) ORDER BY due,id LIMIT 1",
            (now, now),
        ).fetchone()
        if not row:
            return False
        token = uuid.uuid4().hex
        db.execute(
            "UPDATE webhooks SET status='inflight',attempts=attempts+1,lease=?,token=? WHERE id=?",
            (now + 15, token, row["id"]),
        )
    body = {"event_id": row["id"], "receipt": json.loads(row["payload"])}
    stamp, error = int(store.clock()), None
    try:
        http_json(
            store.config["api_url"] + "/webhooks/mock",
            body=body,
            headers={
                "X-Timestamp": str(stamp),
                "X-Signature": signature(
                    store.config["webhook_secret"], stamp, dumps(body).encode()
                ),
            },
            deadline=deadline,
            cancel=cancel,
        )
    except Exception as exc:
        error = str(exc)[:300]
    with store.transaction(True) as db:
        if error is None:
            db.execute(
                "UPDATE webhooks SET status='done',lease=0,token='',error='' WHERE id=? AND status='inflight' AND token=? AND lease>?",
                (row["id"], token, store.clock()),
            )
        else:
            db.execute(
                "UPDATE webhooks SET status='pending',lease=0,token='',due=?,error=? WHERE id=? AND status='inflight' AND token=? AND lease>?",
                (
                    store.clock() + min(30, 0.1 * 2 ** min(row["attempts"], 8)),
                    error,
                    row["id"],
                    token,
                    store.clock(),
                ),
            )
    return True
