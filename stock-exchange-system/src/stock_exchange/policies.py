"""Deterministic synthetic session and submitted-volume risk policies."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone


class SessionPolicy(ABC):
    @abstractmethod
    def permits(self, timestamp, settings):
        pass


class UTCSession(SessionPolicy):
    def permits(self, timestamp, settings):
        moment = datetime.fromtimestamp(timestamp, timezone.utc)
        return (
            moment.weekday() in settings["weekdays"]
            and settings["start_minute"]
            <= moment.hour * 60 + moment.minute
            < settings["end_minute"]
        )


class RiskPolicy(ABC):
    @abstractmethod
    def permits(self, used, quantity, maximum):
        pass


class SubmittedVolumeRisk(RiskPolicy):
    def permits(self, used, quantity, maximum):
        return used + quantity <= maximum
