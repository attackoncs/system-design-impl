import smtplib
import time

from .common import http_json
from .objects import Objects


def run_sender(config, shard):
    url, token = config["primary_urls"][shard], config["internal_token"]
    while True:
        job = None
        try:
            job = http_json(url + "/internal/claim", token, {})["job"]
            if not job:
                time.sleep(0.05)
                continue
            raw = Objects(config).get(job["raw"])
            # No MX lookup or external SMTP destination exists in this adapter.
            with smtplib.SMTP("127.0.0.1", config["smtp_port"], timeout=5) as smtp:
                smtp.login("local-worker", config["smtp_token"])
                refused = smtp.sendmail(job["sender"], job["recipients"], raw)
                if refused:
                    raise RuntimeError("some local recipients refused")
            http_json(url + "/internal/settle", token, {"job": job})
        except Exception as exc:
            if job:
                try:
                    http_json(
                        url + "/internal/settle", token, {"job": job, "error": str(exc)}
                    )
                except Exception:
                    pass
            time.sleep(0.1)


def replicate(config, store, stop, state):
    url = config["primary_urls"][store.shard]
    while not stop.is_set():
        try:
            after = store.status()["position"]
            batch = http_json(
                url + f"/internal/changes?after={after}", config["internal_token"]
            )
            store.apply(batch)
            state.update(error=None, last_poll=time.time())
            stop.wait(0.02 if batch["changes"] else 0.1)
        except Exception as exc:
            state["error"] = str(exc)
            stop.wait(0.2)
