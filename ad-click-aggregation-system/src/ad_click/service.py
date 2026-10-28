from .recovery import reconcile as reconcile
from .recovery import replay as replay
from .query_service import QueryService as QueryService

from urllib.parse import urlencode

from .common import http_json


def log_page(config, partition, after, until=None):
    args = {"partition": partition, "after": after}
    if until is not None:
        args["until"] = until
    return http_json(
        config["ingress_url"] + "/internal/log?" + urlencode(args),
        config["internal_token"],
    )


def aggregate_query(config, body):
    """One-shot compatibility helper; HTTP owns a persistent QueryService."""
    with QueryService(config) as service:
        return service.aggregate(body)
