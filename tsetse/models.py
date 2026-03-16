from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time


class LoopMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"

    def cycle(self) -> "LoopMode":
        order = [LoopMode.OFF, LoopMode.ALL, LoopMode.ONE]
        return order[(order.index(self) + 1) % len(order)]


@dataclass(slots=True)
class Track:
    video_id: str
    title: str
    channel: str = ""
    duration: int | None = None
    watch_url: str = ""
    source: str = "search"

    def __post_init__(self) -> None:
        if not self.watch_url:
            self.watch_url = f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(slots=True)
class StreamCacheEntry:
    url: str
    fetched_at: float = field(default_factory=time.time)
    ttl_seconds: int = 1800

    def is_fresh(self) -> bool:
        return (time.time() - self.fetched_at) < self.ttl_seconds


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "--:--"

    total = int(max(seconds, 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
