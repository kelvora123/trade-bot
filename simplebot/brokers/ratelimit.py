"""A shared token-bucket limiter so every adapter respects its broker's published rate limits."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        capacity: int,
        per_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.rate = self.capacity / float(per_seconds)  # tokens per second
        self.tokens = float(self.capacity)
        self.clock = clock
        self.sleep = sleep
        self.last = clock()
        self.waited = 0.0  # total seconds slept; handy in tests and logs

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def acquire(self) -> None:
        self._refill()
        if self.tokens < 1.0:
            wait = (1.0 - self.tokens) / self.rate
            self.sleep(wait)
            self.waited += wait
            self._refill()
            self.tokens = max(self.tokens, 1.0)
        self.tokens -= 1.0


class EndpointLimiter:
    """One bucket per endpoint key. `limits` maps key -> (calls, per_seconds)."""

    def __init__(
        self,
        limits: dict[str, tuple[int, float]],
        default: tuple[int, float] = (1, 1.0),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limits = dict(limits)
        self.default = default
        self.clock = clock
        self.sleep = sleep
        self.buckets: dict[str, TokenBucket] = {}

    def key_for(self, path: str) -> str:
        matches = [k for k in self.limits if path.startswith(k)]
        return max(matches, key=len) if matches else "__default__"

    def acquire(self, path: str) -> str:
        key = self.key_for(path)
        if key not in self.buckets:
            calls, per = self.limits.get(key, self.default)
            self.buckets[key] = TokenBucket(calls, per, clock=self.clock, sleep=self.sleep)
        self.buckets[key].acquire()
        return key
