import time
from urllib.error import HTTPError

from .common import http_json
from .provider import MockHTTPProvider


def run(config):
    url, token = config["api_url"], config["internal_token"]
    provider = MockHTTPProvider(config)
    while True:
        job = None
        try:
            job = http_json(url + "/internal/claim", token, {})["job"]
            if not job:
                time.sleep(0.05)
                continue
            if job["kind"] == "execute":
                op = http_json(
                    url + "/internal/operation/" + job["payload"]["operation"], token
                )
                if op["status"] in ("SUCCEEDED", "FAILED"):
                    http_json(url + "/internal/finish", token, {"job": job})
                    continue
                try:
                    receipt = provider.query(op["id"])
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    receipt = provider.create(
                        op
                    )  # Same fixed provider identity; never a new charge key.
                applied = http_json(
                    url + "/internal/apply", token, {"receipt": receipt}
                )
                if applied.get("review"):
                    raise ValueError("PSP receipt requires manual review")
                http_json(
                    url + "/internal/finish",
                    token,
                    {"job": job, "pending": receipt["status"] == "PENDING"},
                )
            elif job["kind"] == "notify":
                http_json(
                    config["receiver_url"] + "/events",
                    config["event_token"],
                    job["payload"],
                )
                http_json(url + "/internal/finish", token, {"job": job})
            else:
                statement = http_json(
                    config["provider_url"] + "/statements", config["provider_token"]
                )
                http_json(
                    url + "/internal/reconcile",
                    token,
                    {"run": job["payload"]["run"], "statement": statement},
                )
                http_json(url + "/internal/finish", token, {"job": job})
        except Exception as exc:
            if job:
                try:
                    http_json(
                        url + "/internal/finish", token, {"job": job, "error": str(exc)}
                    )
                except Exception:
                    pass  # Retain durable lease; another worker can reclaim it.
            time.sleep(0.1)
