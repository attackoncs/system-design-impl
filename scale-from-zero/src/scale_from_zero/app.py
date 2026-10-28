import hmac
import threading

from .common import Error, identifier, integer, request
from .store import partition
from .contracts import (
    freeze,
    record,
    update as validate_update,
    session as validate_session,
)
from .runtime import RequestContext
from .assets import ASSETS as ASSETS


class App:
    def __init__(self, config, node, store=None):
        self.config, self.node, self.store = freeze(config), node, store
        self.context = RequestContext()
        self.operation = self.context.operation
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
            **self.context.options(),
        )

    def login(self, body):
        if not isinstance(body, dict) or set(body) != {"user", "password"}:
            raise ValueError("user and password required")
        user = identifier(body["user"])
        if (
            not isinstance(body["password"], str)
            or not body["password"].isascii()
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
        if user not in self.config["users"]:
            raise Error(401, "session user missing")
        try:
            validate_session(user, token)
        except ValueError:
            raise Error(401, "session invalid or expired") from None
        reply = self.primary(user, "session_check", {"token": token})
        if reply != {"user": user}:
            raise Error(502, "session check returned another identity")
        return user

    def cache(self, action, body):
        return request(
            self.config["cache"] + "/internal/" + action,
            self.config["internal_token"],
            body,
            timeout=0.4,
            **self.context.options(),
        )

    def update(self, user, body):
        body = validate_update(body)
        value = self.validate_record(
            self.primary(user, "update", {"profile": body}), user
        )
        if value["version"] != body["expected_version"] + 1 or value["profile"] != {
            key: body[key] for key in ("display_name", "city")
        }:
            raise Error(502, "update response differs from the submitted operation")
        if self.store is None:
            try:
                self.cache("delete", {"key": user, "min_version": value["version"]})
            except (Error, OSError, TimeoutError):
                self._record_cache_error()
        return {**value, "web_node": self.node}

    @staticmethod
    def validate_record(value, user):
        try:
            return record(value, user)
        except (ValueError, TypeError, KeyError):
            raise Error(502, "invalid profile response identity/version") from None

    def get(self, user, strong=False, min_version=0):
        integer(min_version)
        source, value, generation, floor = "primary", None, None, min_version
        if self.store is not None or strong:
            value = self.validate_record(self.primary(user, "get", {}), user)
        else:
            try:
                probe = self.cache("get", {"key": user, "min_version": min_version})
                floor = integer(probe["min_version"], min_version)
                generation = probe["generation"]
                if not isinstance(generation, str) or len(generation) > 80:
                    raise ValueError("cache generation required")
                if probe["value"] is not None:
                    value = self.validate_record(probe["value"], user)
                    if value["version"] < floor:
                        raise ValueError("cache below requested version")
            except (Error, OSError, ValueError, TypeError, KeyError):
                self._record_cache_error()
                value, generation = None, None
            if value is not None:
                source = "cache"
            else:
                index = partition(user, len(self.config["primaries"]))
                try:
                    value = self.validate_record(
                        request(
                            self.config["replicas"][index] + "/internal/get",
                            self.config["internal_token"],
                            {"user": user},
                            timeout=0.5,
                            **self.context.options(),
                        ),
                        user,
                    )
                    if value["version"] < floor:
                        value = None
                    else:
                        source = "replica"
                except (Error, OSError):
                    value = None
                if value is None:
                    value = self.validate_record(self.primary(user, "get", {}), user)
                # A failed probe has no safe generation ticket; bypass filling.
                if generation is not None:
                    try:
                        self.cache(
                            "put",
                            {"key": user, "value": value, "generation": generation},
                        )
                    except (Error, OSError):
                        self._record_cache_error()
        if value["version"] < floor:
            raise Error(409, "requested version does not exist yet")
        return {**value, "source": source, "web_node": self.node}
