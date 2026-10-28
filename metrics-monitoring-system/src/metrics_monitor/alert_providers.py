"""Explicit notification adapters; unconfigured external channels fail visibly."""

from abc import ABC, abstractmethod
import json
from types import MappingProxyType

from .common import dumps, http_json, identifier


class Provider(ABC):
    @abstractmethod
    def deliver(self, destination, channel, payload, key):
        """Return only after provider confirmation; retries reuse key."""


class WebhookProvider(Provider):
    def __init__(self, destinations):
        if not isinstance(destinations, dict) or len(destinations) > 1000:
            raise ValueError("at most 1000 notification destinations required")
        captured = json.loads(dumps(destinations))
        for destination, target in captured.items():
            identifier(destination)
            if (
                not isinstance(target, dict)
                or set(target) - {"url", "token"}
                or not isinstance(target.get("url"), str)
                or not target["url"].startswith(("http://", "https://"))
                or (
                    target.get("token") is not None
                    and not isinstance(target["token"], str)
                )
            ):
                raise ValueError("invalid webhook destination")
        self._destinations = MappingProxyType(
            {key: MappingProxyType(value) for key, value in captured.items()}
        )

    @property
    def destinations(self):
        return {key: dict(value) for key, value in self._destinations.items()}

    def deliver(self, destination, channel, payload, key):
        if channel != "webhook":
            raise RuntimeError("email/phone/PagerDuty adapter not configured")
        target = self._destinations.get(destination)
        if not target:
            raise RuntimeError("notification destination not configured")
        return http_json(
            target["url"],
            "POST",
            payload,
            token=target.get("token"),
            headers={"Idempotency-Key": key},
        )
