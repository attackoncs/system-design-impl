import time

from .common import http_json
from .payment import SimulatedHTTPPayment


def run(config, shard):
    url, token = config["shard_urls"][shard], config["internal_token"]
    provider = SimulatedHTTPPayment(config)
    while True:
        job = None
        try:
            http_json(url + "/internal/sweep", token, {})
            job = http_json(url + "/internal/claim", token, {})["job"]
            if job is None:
                time.sleep(0.05)
                continue
            result = provider.execute(job)
            http_json(url + "/internal/finish", token, {"job": job, "result": result})
        except Exception as exc:
            if job:
                try:
                    http_json(url + "/internal/retry", token, {"job": job, "error": str(exc)})
                except Exception:
                    pass  # The durable lease will expire; no job is acknowledged.
            time.sleep(0.1)
