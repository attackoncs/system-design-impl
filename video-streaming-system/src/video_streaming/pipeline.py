"""Validated, reproducible DAG generation from trusted built-in task templates."""

from dataclasses import dataclass
from typing import Optional, Tuple

PROFILES = {
    144: (256, 144, 200000),
    240: (426, 240, 400000),
    360: (640, 360, 800000),
    480: (854, 480, 1400000),
    720: (1280, 720, 2800000),
    1080: (1920, 1080, 5000000),
}
KINDS = frozenset({"inspect", "preprocess", "thumbnail", "encode", "publish"})


@dataclass(frozen=True)
class Task:
    name: str
    kind: str
    profile: Optional[int] = None
    dependencies: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Pipeline:
    """Configure rendition/thumbnail/GOP stages without arbitrary commands.

    The generated tasks, dependencies and rendition choices are persisted at
    finalization. Workers on restart consume that plan, not startup defaults.
    """

    profiles: Tuple[int, ...] = (240, 360)
    thumbnail: bool = True
    preprocess: bool = False

    def __post_init__(self):
        if not isinstance(self.profiles, (tuple, list)) or not self.profiles:
            raise ValueError("profiles must be a nonempty list or tuple")
        profiles = tuple(self.profiles)
        if any(
            type(profile) is not int or profile not in PROFILES for profile in profiles
        ):
            raise ValueError("unsupported rendition")
        if len(profiles) != len(set(profiles)):
            raise ValueError("duplicate rendition")
        if type(self.thumbnail) is not bool or type(self.preprocess) is not bool:
            raise ValueError("pipeline switches must be booleans")
        object.__setattr__(self, "profiles", profiles)

    @classmethod
    def from_config(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or set(value) - {
            "profiles",
            "thumbnail",
            "preprocess",
        }:
            raise ValueError("invalid pipeline configuration")
        return cls(**value)

    def tasks(self):
        """Return a topological, immutable DAG for the built-in operations."""
        tasks = [Task("inspect", "inspect")]
        predecessor = "inspect"
        if self.preprocess:
            tasks.append(Task("preprocess", "preprocess", dependencies=(predecessor,)))
            predecessor = "preprocess"
        if self.thumbnail:
            tasks.append(Task("thumbnail", "thumbnail", dependencies=(predecessor,)))
        tasks.extend(
            Task(f"encode-{p}", "encode", p, (predecessor,)) for p in self.profiles
        )
        terminals = tuple(
            task.name for task in tasks if task.kind in ("encode", "thumbnail")
        )
        tasks.append(Task("publish", "publish", dependencies=terminals))
        return tuple(tasks)
