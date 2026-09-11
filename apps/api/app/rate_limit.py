from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable


class InMemorySlidingWindowRateLimiter:
    """Per-key sliding-window rate limiter.

    In-process only, deliberately: this project pins `minReplicas:
    maxReplicas: 1` (OD-22) because `app.state.requests` (live cancellation-
    task references) has no cross-replica representation -- this limiter has
    the exact same limitation, for the same reason, and is not a real
    distributed rate limiter if that pin is ever lifted without also
    replacing this with a shared store (e.g. Redis).

    `clock` is injectable so tests can control time deterministically
    without sleeping (mirrors this project's factory-injection discipline,
    D-007) -- real usage defaults to `time.monotonic`, immune to wall-clock
    adjustments.
    """

    def __init__(self, *, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> tuple[bool, float]:
        """Returns `(allowed, retry_after_seconds)`. `retry_after_seconds`
        is `0.0` when allowed; otherwise the time until the oldest hit in
        the current window ages out and a new request would be admitted.
        """
        now = self._clock()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False, max(hits[0] + self.window_seconds - now, 0.0)
        hits.append(now)
        return True, 0.0
