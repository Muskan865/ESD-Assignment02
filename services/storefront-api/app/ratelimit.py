"""In-process token-bucket rate limiting (single instance; Assignment 2 Part A).

Each client key owns a bucket holding up to `burst` tokens, refilled at `rate`
tokens per second. A request spends one token; with none left it is refused
with 429 and a Retry-After of the whole seconds until a token is back.

State lives in this process only: with two storefront replicas each would
enforce its own limit, and a restart forgets every bucket. A shared store
(Redis) would be the next step; one container is all this project runs.
"""

import math
import threading
import time
from dataclasses import dataclass


@dataclass
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int  # whole seconds; 0 when allowed


class TokenBucketLimiter:
    # Buckets idle long enough to be full again are forgotten, so the dict
    # cannot grow without bound however many client addresses appear.
    _PRUNE_EVERY = 1000

    def __init__(self, rate: float, burst: int, clock=time.monotonic):
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, updated_at)
        self._calls = 0

    def take(self, key: str) -> Decision:
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - updated) * self.rate)
            if tokens >= 1.0:
                tokens -= 1.0
                decision = Decision(True, self.burst, int(tokens), 0)
            else:
                wait = (1.0 - tokens) / self.rate
                decision = Decision(False, self.burst, 0, max(1, math.ceil(wait)))
            self._buckets[key] = (tokens, now)

            self._calls += 1
            if self._calls % self._PRUNE_EVERY == 0:
                self._prune(now)
        return decision

    def _prune(self, now: float) -> None:
        full_after = self.burst / self.rate
        for key, (_, updated) in list(self._buckets.items()):
            if now - updated > full_after:
                del self._buckets[key]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
