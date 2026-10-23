import hmac
import threading

from .common import Error, identifier, integer, request
from .store import partition


class App:
    def __init__(self, config, node, store=None):
        self.config, self.node, self.store = config, node, store
        self._cache_errors = 0
        self._metrics_lock = threading.Lock()

    @property
    def cache_errors(self):
        with self._metrics_lock:
            return self._cache_errors

    def _record_cache_error(self):
        with self._metrics_lock:
            self._cache_errors += 1

    def primary(self, user, action, body):
        if self.store is not None:
            if action == "session_create":
                return self.store.session_create(user)
            if action == "session_check":
                return self.store.session_check(user, body["token"])
            if action == "get":
                return self.store.get(user)
            return self.store.update(user, body["profile"])
        url = self.config["primaries"][partition(user, len(self.config["primaries"]))]
        return request(
            url + "/internal/" + action,
            self.config["internal_token"],
            {"user": user, **body},
            timeout=1,
        )

    def login(self, body):
        if not isinstance(body, dict) or set(body) != {"user", "password"}:
            raise ValueError("user and password required")
        user = identifier(body["user"])
        if (
            not isinstance(body["password"], str)
            or not hmac.compare_digest(
                self.config["users"].get(user, ""), body["password"]
            )
            or user not in self.config["users"]
        ):
            raise Error(401, "invalid lab credentials")
        return {**self.primary(user, "session_create", {}), "web_node": self.node}

    def authorize(self, header):
        if not header.startswith("Bearer ") or len(header) > 256:
            raise Error(401, "session bearer required")
        token = header[7:]
        user = identifier(token.split(".", 1)[0])
        self.primary(user, "session_check", {"token": token})
        return user

    def cache(self, action, body):
        return request(
            self.config["cache"] + "/internal/" + action,
            self.config["internal_token"],
            body,
            timeout=0.4,
        )

    def update(self, user, body):
        value = self.primary(user, "update", {"profile": body})
        if self.store is None:
            try:
                self.cache("delete", {"key": user})
            except (Error, OSError, TimeoutError):
                self._record_cache_error()
        return {**value, "web_node": self.node}

    def get(self, user, strong=False, min_version=0):
        integer(min_version)
        value = None
        source = "primary"
        if self.store is not None or strong:
            value = self.primary(user, "get", {})
        else:
            try:
                value = self.cache("get", {"key": user, "min_version": min_version})[
                    "value"
                ]
            except (Error, OSError, TimeoutError):
                self._record_cache_error()
            if value:
                source = "cache"
            else:
                index = partition(user, len(self.config["primaries"]))
                try:
                    value = request(
                        self.config["replicas"][index] + "/internal/get",
                        self.config["internal_token"],
                        {"user": user},
                        timeout=0.5,
                    )
                    if value["version"] < min_version:
                        value = None
                    else:
                        source = "replica"
                except (Error, OSError, TimeoutError):
                    value = None
                if value is None:
                    value = self.primary(user, "get", {})
                try:
                    self.cache("put", {"key": user, "value": value})
                except (Error, OSError, TimeoutError):
                    self._record_cache_error()
        if value["version"] < min_version:
            raise Error(409, "requested version does not exist yet")
        return {**value, "source": source, "web_node": self.node}


ASSETS = {
    "app-v1.txt": "Synthetic static asset, version one.",
    "app-v2.txt": "Synthetic static asset, version two.",
}
